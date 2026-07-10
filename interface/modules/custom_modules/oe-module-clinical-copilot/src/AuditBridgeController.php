<?php

/**
 * Clinical Co-Pilot audit-bridge controller (T016).
 *
 * The decision/disclosure log that OpenEMR's native access log lacks: it records
 * that an AI mediated a patient access, what the verification verdict was, and —
 * via EventAuditLogger::recordDisclosure() — the §164.528 disclosure accounting
 * for the chart data sent to the LLM provider.
 *
 * Failure directions (deliberate, learned the expensive way — see
 * .tdd-swarm/LESSONS.md and ARCHITECTURE.md §7/§9):
 *   - This endpoint is the SERVER of a fail-open client (T013). Every non-2xx it
 *     returns destroys an audit record permanently (no retry). So it rejects only
 *     when a row cannot be attributed truthfully, never for tidiness.
 *   - A 2xx is a promise the row was written; it is returned only after a write.
 *   - Rejections never build an oracle: all auth failures render ONE byte-identical
 *     401 body, all record/patient failures ONE byte-identical 422 body.
 *   - Leaks hide in the guard: no request body, bearer, or exception message ever
 *     reaches a log or the client — the exception class name and correlation id only.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    OpenEMR Clinical Co-Pilot <support@open-emr.org>
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use DateTimeZone;
use League\OAuth2\Server\Exception\OAuthServerException;
use League\OAuth2\Server\ResourceServer;
use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Auth\OpenIDConnect\Repositories\AccessTokenRepository;
use OpenEMR\Common\Auth\UuidUserAccount;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Logging\EventAuditLogger;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\FHIR\Config\ServerConfig;
use Psr\Log\LoggerInterface;
use RuntimeException;
use Symfony\Bridge\PsrHttpMessage\Factory\PsrHttpFactory;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\HttpFoundation\Session\SessionInterface;

final class AuditBridgeController
{
    public const MODULE_DIRECTORY = 'oe-module-clinical-copilot';
    public const EVENT_TYPE = 'ai-clinical-summary';
    public const LOG_FROM = 'clinical-copilot';

    /** Default recipient identity for §164.528 accounting; overridable via globals. */
    public const LLM_PROVIDER_IDENTITY = 'Anthropic Claude (BAA-covered)';
    public const PROVIDER_IDENTITY_GLOBAL = 'copilot_llm_provider_identity';

    private const BODY_UNAUTHORIZED = ['error' => 'unauthorized'];
    private const BODY_UNPROCESSABLE = ['error' => 'unprocessable'];

    private readonly LoggerInterface $logger;
    private readonly EventAuditLogger $auditLogger;

    public function __construct(?LoggerInterface $logger = null, ?EventAuditLogger $auditLogger = null)
    {
        $this->logger = $logger ?? ServiceContainer::getLogger();
        $this->auditLogger = $auditLogger ?? EventAuditLogger::getInstance();
    }

    public function handle(Request $request, SessionInterface $session): Response
    {
        try {
            if ($request->getMethod() !== 'POST') {
                return new JsonResponse(['error' => 'method_not_allowed'], Response::HTTP_METHOD_NOT_ALLOWED);
            }

            // No modules-table gate here: core enforces "disabled module ⇒ no
            // endpoint" one line after this file's require_once. globals.php:748
            // constructs ModulesApplication unconditionally ($ignoreAuth does not
            // bypass it — that block closes at :728); its
            // checkModuleScriptPathForEnabledModule() throws AccessDeniedException
            // for any SCRIPT_NAME under a module dir with no active row, which
            // globals.php:754-758 answers with http_response_code(401) + exit(1).
            // So this controller never executes for a disabled module; a
            // modules-table query here would be dead code behind a gate that
            // already fired.
            $auth = $this->authenticate($request, $session);
            if ($auth === null) {
                return $this->unauthorized();
            }
            [$username, $rawToken] = $auth;

            // Caller-capability check; does not depend on the patient, so folding
            // it into the 401 leaks nothing about any patient's existence.
            if (!AclMain::aclCheckCore('patients', 'demo', $username)) {
                return $this->unauthorized();
            }

            $record = AuditRecordParser::parse($request->getContent());
            if ($record === null) {
                return $this->unprocessable();
            }

            // Binding check: the record must be attributed to the presenting
            // token's user, not a different user's session. Gate before patient
            // resolution so a mismatched caller never learns whether a patient exists.
            $expectedHash = hash('sha256', $rawToken);
            if (!hash_equals($expectedHash, $record->userTokenHash)) {
                return $this->unauthorized();
            }

            $pid = $this->resolvePid($record->patientId);
            if ($pid === null) {
                return $this->unprocessable();
            }

            $this->writeAudit($record, $pid, $username);

            return new JsonResponse(['status' => 'recorded'], Response::HTTP_CREATED);
        } catch (RuntimeException $e) {
            // Recoverable-layer failure only — e.g. a DB read raising
            // SqlQueryException (extends RuntimeException) during patient
            // resolution or token/ACL lookups. Narrowed to RuntimeException so a
            // programming \Error (\TypeError etc.) is never swallowed here: it
            // propagates to globals.php's handler, which answers HTTP 500 with a
            // generic body — non-2xx, so T013's fail-open client counts the audit
            // write as failed rather than falsely successful.
            //
            // The guard must never become the leak: class name + correlation only,
            // never the exception message (our messages carry SQL and payloads).
            $this->logger->error('clinical_copilot_audit_bridge_error', [
                'exception_class' => $e::class,
            ]);
            return new JsonResponse(['error' => 'server_error'], Response::HTTP_INTERNAL_SERVER_ERROR);
        }
    }

    private function unauthorized(): JsonResponse
    {
        return new JsonResponse(self::BODY_UNAUTHORIZED, Response::HTTP_UNAUTHORIZED);
    }

    private function unprocessable(): JsonResponse
    {
        return new JsonResponse(self::BODY_UNPROCESSABLE, Response::HTTP_UNPROCESSABLE_ENTITY);
    }

    /**
     * Validate the bearer path-independently (see the ticket: BearerToken
     * strategy is path-coupled and cannot be reused directly). Returns
     * [username, rawToken] on success, null on every auth failure.
     *
     * @return array{0: string, 1: string}|null
     */
    private function authenticate(Request $request, SessionInterface $session): ?array
    {
        $authHeader = $request->headers->get('Authorization');
        if (!is_string($authHeader) || !str_starts_with($authHeader, 'Bearer ')) {
            return null;
        }
        $rawToken = substr($authHeader, 7);
        if ($rawToken === '') {
            return null;
        }

        $serverConfig = new ServerConfig();
        $repository = new AccessTokenRepository($serverConfig, $session);

        // getPublicRestKey() has no declared return type and PHPStan widens it to
        // mixed; narrow to the file-path string ResourceServer expects rather than
        // casting. A non-string here would be a server misconfiguration, not a bad
        // bearer, so it fails closed to 401 (never distinguishable by the caller).
        $publicKey = $serverConfig->getPublicRestKey();
        if (!is_string($publicKey)) {
            return null;
        }

        try {
            $server = new ResourceServer($repository, $publicKey);
            $psrRequest = $this->psrHttpFactory()->createRequest($request);
            $validated = $server->validateAuthenticatedRequest($psrRequest);
        } catch (OAuthServerException) {
            // league throws this for absent, malformed, expired, and revoked
            // bearers — all one 401. Narrowed from Throwable so a \TypeError in
            // this block is no longer misreported to the caller as "bad token";
            // it propagates as a 500 (the bug the ticket calls out).
            return null;
        }

        $attributes = $validated->getAttributes();
        $userUuid = $attributes['oauth_user_id'] ?? null;
        $tokenId = $attributes['oauth_access_token_id'] ?? null;
        if (!is_string($userUuid) || $userUuid === '' || !is_string($tokenId) || $tokenId === '') {
            return null;
        }

        if ($repository->isAccessTokenRevokedInDatabase($tokenId)) {
            return null;
        }

        $account = new UuidUserAccount($userUuid);
        if ($account->getUserRole() !== 'users') {
            return null;
        }
        $user = $account->getUserAccount();
        $username = $user['username'] ?? null;
        if (!is_string($username) || $username === '') {
            return null;
        }

        return [$username, $rawToken];
    }

    private function psrHttpFactory(): PsrHttpFactory
    {
        return new PsrHttpFactory(
            ServiceContainer::getServerRequestFactory(),
            ServiceContainer::getStreamFactory(),
            ServiceContainer::getUploadedFileFactory(),
            ServiceContainer::getResponseFactory(),
        );
    }

    /**
     * Resolve the record's FHIR Patient uuid string to a local pid. An
     * unresolvable uuid (bad format or no such patient) is null → 422.
     */
    private function resolvePid(string $patientUuid): ?int
    {
        if (!UuidRegistry::isValidStringUUID($patientUuid)) {
            return null;
        }
        $bytes = UuidRegistry::uuidToBytes($patientUuid);
        $pid = QueryUtils::fetchSingleValue(
            "SELECT `pid` FROM `patient_data` WHERE `uuid` = ?",
            'pid',
            [$bytes]
        );
        // fetchSingleValue returns mixed; narrow rather than cast. The pid column
        // is an integer, but DBAL may hand it back as a numeric string.
        if (is_int($pid)) {
            return $pid;
        }
        if (is_string($pid) && ctype_digit($pid)) {
            return (int) $pid;
        }
        return null;
    }

    /**
     * Write the decision log then the disclosure row. Order matters: an
     * invocation logged without disclosure accounting is reconstructable from
     * `log`, whereas a disclosure row with no invocation row is not.
     */
    private function writeAudit(AuditRecord $record, int $pid, string $username): void
    {
        $comments = $this->buildComments($record);

        // Write the decision log through recordLogItem — the same gate-free sink
        // EventAuditLogger::newEvent() itself wraps (newEvent → recordLogItem →
        // sink, with no AuditConfig::isEventTypeEnabled() gate, unlike
        // auditSQLEvent which would silently drop the unregistered
        // 'ai-clinical-summary' type). recordLogItem is used directly rather
        // than newEvent because newEvent forwards $log_from to the sink ONLY on
        // its 'patient-portal' branch and otherwise drops it — leaving log_from
        // as the 'open-emr' default. Criterion 3 requires log_from =
        // 'clinical-copilot', so the value must be passed at the sink boundary.
        //
        // $success = 1 for all three outcomes: it records that the invocation
        // was authorized, not that the model answered well — the outcome lives
        // in $comments / $description. $category mirrors newEvent's default
        // ($category = $event).
        $this->auditLogger->recordLogItem(
            1,
            self::EVENT_TYPE,
            $username,
            '',
            $comments,
            $pid,
            self::EVENT_TYPE,
            self::LOG_FROM
        );

        $dates = $record->occurredAt
            ->setTimezone(new DateTimeZone(date_default_timezone_get()))
            ->format('Y-m-d H:i:s');
        $description = $this->buildDisclosureDescription($record);

        // Note recordDisclosure's argument order: $pid is 3rd arg, $user is 6th.
        $this->auditLogger->recordDisclosure(
            $dates,
            self::EVENT_TYPE,
            $pid,
            $this->providerIdentity(),
            $description,
            $username
        );
    }

    private function buildComments(AuditRecord $record): string
    {
        return (string) json_encode([
            'correlation_id' => $record->correlationId,
            'conversation_id' => $record->conversationId,
            'outcome' => $record->outcome,
            'claims_total' => $record->claimsTotal,
            'claims_passed' => $record->claimsPassed,
            'claims_stripped' => $record->claimsStripped,
            'degraded' => $record->degraded,
            'fallback_reason' => $record->fallbackReason,
        ]);
    }

    private function buildDisclosureDescription(AuditRecord $record): string
    {
        return sprintf(
            'AI clinical co-pilot invocation; patient chart data disclosed to LLM provider for '
            . 'clinical summarization. outcome=%s correlation_id=%s conversation_id=%s '
            . 'claims_total=%d claims_passed=%d claims_stripped=%d',
            $record->outcome,
            $record->correlationId,
            $record->conversationId,
            $record->claimsTotal,
            $record->claimsPassed,
            $record->claimsStripped
        );
    }

    private function providerIdentity(): string
    {
        $bag = OEGlobalsBag::getInstance();
        if ($bag->has(self::PROVIDER_IDENTITY_GLOBAL)) {
            $override = $bag->getString(self::PROVIDER_IDENTITY_GLOBAL);
            if ($override !== '') {
                return $override;
            }
        }
        return self::LLM_PROVIDER_IDENTITY;
    }
}
