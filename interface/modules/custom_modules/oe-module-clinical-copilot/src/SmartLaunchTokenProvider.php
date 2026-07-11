<?php

/**
 * Per-user / per-patient SMART launch token provider for the co-pilot relay (T027).
 *
 * Replaces T021's service-account bearer. Mints, ENTIRELY SERVER-SIDE (no browser
 * redirect, no autosubmit, no consent screen), a signed OAuth2 access token bound
 * to the ACTUAL logged-in clinician (from the relay session) and the ACTUAL open
 * patient (passed in from the relay call site). Because the token's oauth_user_id
 * is the clinician's uuid, T016's audit / §164.528 disclosure rows — and the FHIR
 * api_log — name the real human instead of `admin`.
 *
 * The mint reuses the exact core token path (AccessTokenRepository, OAuth2KeyConfig,
 * CryptKey, BearerTokenResponse, TrustedUserService) that
 * src/Common/Command/GenerateAccessTokenCommand.php uses, minus its CLI password
 * gate: the relay already runs inside the clinician's authenticated session, so it
 * supplies the clinician uuid rather than a password. This coupling to core OAuth
 * internals is inherent to minting without the HTTP grant endpoints, not a smell.
 *
 * Scope shape is load-bearing: the token carries ONLY patient/* resource scopes
 * (the eight the agent's FHIR tools need) plus `openid` + `launch`, and NO user/*
 * scope. A stray user/* scope silently widens FHIR reads to all patients and
 * defeats the launch-patient binding. NOTE: this patient binding is a
 * scoping/correctness measure, not an authorization boundary — OpenEMR has no
 * per-patient ACL and the relay's SessionPatientAccessGuard remains the gate.
 *
 * Fail-closed guardrails: a token is minted only for a user the T016 audit bridge
 * will accept (role `users` and passing aclCheckCore('patients','demo')). For any
 * other user the provider throws AgentUnavailableException — the relay renders its
 * graceful "couldn't reach the assistant" state — rather than minting a token the
 * bridge would silently drop, which would re-open the audit-attribution gap.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use DateInterval;
use DateTimeImmutable;
use League\OAuth2\Server\CryptKey;
use League\OAuth2\Server\ResponseTypes\BearerTokenResponse;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Auth\OAuth2KeyConfig;
use OpenEMR\Common\Auth\OpenIDConnect\Entities\ClientEntity;
use OpenEMR\Common\Auth\OpenIDConnect\Entities\ScopeEntity;
use OpenEMR\Common\Auth\OpenIDConnect\Repositories\AccessTokenRepository;
use OpenEMR\Common\Auth\OpenIDConnect\Repositories\ClientRepository;
use OpenEMR\Common\Auth\UuidUserAccount;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Http\Psr17Factory;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\FHIR\Config\ServerConfig;
use OpenEMR\Services\TrustedUserService;
use OpenEMR\Services\UserService;
use Psr\Log\NullLogger;
use RuntimeException;
use Symfony\Component\HttpFoundation\Session\Session;
use Symfony\Component\HttpFoundation\Session\Storage\MockFileSessionStorage;

final class SmartLaunchTokenProvider implements ServiceTokenProvider
{
    /** Marker name the single dedicated launch/patient OAuth2 client is registered under. */
    public const CLIENT_NAME = 'oe-module-clinical-copilot smart-launch service';

    /** The eight FHIR resource types the agent's tools read, patient-scoped. */
    private const RESOURCE_TYPES = [
        'Patient', 'Observation', 'Condition', 'AllergyIntolerance',
        'MedicationRequest', 'Encounter', 'DocumentReference', 'Immunization',
    ];

    public function __construct(
        private readonly int $clinicianUserId,
        private readonly string $oauthBaseUrl = 'https://localhost',
        private readonly int $timeoutSeconds = 15,
    ) {
    }

    public function getToken(string $patientUuid): string
    {
        $clinicianUuid = $this->resolveEligibleClinicianUuid();
        $clientId = $this->ensureProvisionedClientId();
        return $this->mint($clientId, $clinicianUuid, $patientUuid);
    }

    /**
     * Resolve the session clinician's uuid, but only if they satisfy the SAME
     * guardrails the T016 audit bridge enforces (role `users` and
     * aclCheckCore('patients','demo')). Otherwise fail closed: the bridge would
     * drop the token, re-opening the audit gap this provider exists to close.
     */
    private function resolveEligibleClinicianUuid(): string
    {
        if ($this->clinicianUserId <= 0) {
            throw new AgentUnavailableException('no acting clinician in session');
        }

        $user = (new UserService())->getUser($this->clinicianUserId);
        if (!is_array($user)) {
            throw new AgentUnavailableException('acting clinician could not be resolved');
        }
        $username = $user['username'] ?? null;
        $rawUuid = $user['uuid'] ?? null;
        if (!is_string($username) || $username === '' || $rawUuid === null || $rawUuid === '') {
            throw new AgentUnavailableException('acting clinician has no username or uuid');
        }
        $clinicianUuid = (is_string($rawUuid) && strlen($rawUuid) !== 16)
            ? $rawUuid
            : UuidRegistry::uuidToString($rawUuid);

        $account = new UuidUserAccount($clinicianUuid);
        if ($account->getUserRole() !== 'users') {
            throw new AgentUnavailableException('acting user is not a clinical user');
        }
        if (!AclMain::aclCheckCore('patients', 'demo', $username)) {
            throw new AgentUnavailableException('acting user lacks patient demographics access');
        }

        return $clinicianUuid;
    }

    /**
     * Sign, persist, and return a bearer bound to $clinicianUuid + $patientUuid.
     */
    private function mint(string $clientId, string $clinicianUuid, string $patientUuid): string
    {
        $repository = new ClientRepository();
        $repository->setSystemLogger(new NullLogger());
        $client = $repository->getClientEntity($clientId);
        if (!$client instanceof ClientEntity || !$client->isEnabled()) {
            throw new AgentUnavailableException('launch client is not available');
        }

        $scopeStrings = $this->scopeStrings();
        $scopes = array_map(ScopeEntity::createFromString(...), $scopeStrings);
        $scopeIdentifiers = array_map(static fn($scope): string => $scope->getIdentifier(), $scopes);

        $session = new Session(new MockFileSessionStorage());
        $session->set('trusted', 1);
        $accessTokenRepository = new AccessTokenRepository(new ServerConfig(), $session);

        $token = $accessTokenRepository->getNewToken($client, $scopes);
        // Bind the open patient as the SMART launch context.
        $accessTokenRepository->setContextForNewTokens(['patient' => $patientUuid]);
        $token->setExpiryDateTime((new DateTimeImmutable())->add(new DateInterval('PT1H')));

        $keyConfig = new OAuth2KeyConfig($this->siteDirectory());
        $keyConfig->configKeyPairs();
        $privateKey = new CryptKey($keyConfig->getPrivateKeyLocation(), $keyConfig->getPassPhrase());
        $token->setPrivateKey($privateKey);

        // THE attribution binding: the acting clinician, not the service account.
        $token->setUserIdentifier($clinicianUuid);
        $token->setIdentifier(bin2hex(random_bytes(40)));
        $accessTokenRepository->persistNewAccessToken($token);

        // The FHIR bearer check rejects the token without a trusted-user row.
        (new TrustedUserService())->saveTrustedUser(
            $clientId,
            $clinicianUuid,
            $scopeIdentifiers,
            1,
            '',
            (string) json_encode($session->all()),
            'password_grant'
        );

        $bearerResponse = new BearerTokenResponse();
        $bearerResponse->setEncryptionKey($keyConfig->getEncryptionKey());
        $bearerResponse->setAccessToken($token);
        $bearerResponse->setPrivateKey($privateKey);
        $httpResponse = $bearerResponse->generateHttpResponse((new Psr17Factory())->createResponse());
        $httpResponse->getBody()->rewind();
        $decoded = json_decode($httpResponse->getBody()->getContents(), true);
        $accessToken = is_array($decoded) ? ($decoded['access_token'] ?? null) : null;
        if (!is_string($accessToken) || $accessToken === '') {
            throw new AgentUnavailableException('token minting produced no access_token');
        }

        return $accessToken;
    }

    /**
     * @return list<string>
     */
    private function scopeStrings(): array
    {
        $scopes = ['openid', 'launch'];
        foreach (self::RESOURCE_TYPES as $resource) {
            $scopes[] = 'patient/' . $resource . '.read';
        }
        return $scopes;
    }

    private function siteDirectory(): string
    {
        $dir = OEGlobalsBag::getInstance()->get('OE_SITE_DIR');
        if (!is_string($dir) || $dir === '') {
            throw new AgentUnavailableException('site directory is not configured');
        }
        return $dir;
    }

    private function ensureProvisionedClientId(): string
    {
        $existing = $this->lookupEnabledClientId();
        if ($existing !== null) {
            return $existing;
        }
        $clientId = $this->register();
        $this->enable($clientId);
        return $clientId;
    }

    private function lookupEnabledClientId(): ?string
    {
        $rows = QueryUtils::fetchRecords(
            'SELECT `client_id` FROM `oauth_clients` WHERE `client_name` = ? AND `is_enabled` = 1 '
            . 'ORDER BY `register_date` DESC LIMIT 1',
            [self::CLIENT_NAME]
        );
        $clientId = $rows[0]['client_id'] ?? null;
        return is_string($clientId) && $clientId !== '' ? $clientId : null;
    }

    private function register(): string
    {
        $response = $this->httpJson(
            '/oauth2/default/registration',
            [
                'application_type' => 'private',
                'redirect_uris' => ['https://localhost/callback'],
                'client_name' => self::CLIENT_NAME,
                'token_endpoint_auth_method' => 'client_secret_post',
                'contacts' => ['clinical-copilot@example.org'],
                'scope' => implode(' ', $this->scopeStrings()),
                // A SMART launch/patient client uses authorization_code; the
                // grant type does not gate the server-side mint, but registering
                // it keeps the client's declared grants semantically correct.
                'grant_types' => ['authorization_code', 'refresh_token'],
            ]
        );
        $clientId = $response['client_id'] ?? null;
        if (!is_string($clientId) || $clientId === '') {
            throw new AgentUnavailableException('launch client registration returned no client_id');
        }
        return $clientId;
    }

    private function enable(string $clientId): void
    {
        try {
            $repository = new ClientRepository();
            $repository->setSystemLogger(new NullLogger());
            $entity = $repository->getClientEntity($clientId);
            if (!$entity instanceof ClientEntity) {
                throw new AgentUnavailableException('launch client could not be loaded for enablement');
            }
            $repository->saveIsEnabled($entity, true);
        } catch (RuntimeException $e) {
            throw new AgentUnavailableException('launch client could not be enabled', 0, $e);
        }
    }

    /**
     * @param array<string, mixed> $body
     * @return array<string, mixed>
     */
    private function httpJson(string $path, array $body): array
    {
        $encoded = json_encode($body);
        if ($encoded === false) {
            throw new AgentUnavailableException('failed to encode oauth request');
        }

        $handle = curl_init(rtrim($this->oauthBaseUrl, '/') . $path);
        if ($handle === false) {
            throw new AgentUnavailableException('failed to initialize oauth transport');
        }
        curl_setopt_array($handle, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $encoded,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json', 'Accept: application/json'],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_SSL_VERIFYPEER => false,
            CURLOPT_SSL_VERIFYHOST => false,
            CURLOPT_CONNECTTIMEOUT => $this->timeoutSeconds,
            CURLOPT_TIMEOUT => $this->timeoutSeconds,
        ]);
        $raw = curl_exec($handle);
        $errno = curl_errno($handle);
        $status = (int) curl_getinfo($handle, CURLINFO_HTTP_CODE);

        if ($errno !== 0 || !is_string($raw)) {
            throw new AgentUnavailableException('oauth transport error (' . $errno . ')');
        }
        if ($status < 200 || $status >= 300) {
            throw new AgentUnavailableException('oauth endpoint returned status ' . $status);
        }
        $decoded = json_decode($raw, true);
        if (!is_array($decoded)) {
            throw new AgentUnavailableException('oauth endpoint returned an unparseable response');
        }
        /** @var array<string, mixed> $decoded */
        return $decoded;
    }
}
