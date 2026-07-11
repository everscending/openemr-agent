<?php

/**
 * Clinical Co-Pilot per-user / per-patient SMART launch token API test (T027).
 *
 * Exercises the productionized server-side mint that replaces T021's
 * service-account bearer, end to end against the live dev stack (no mocks on the
 * token -> FHIR -> audit path). The provider mints a signed OAuth2 access token
 * bound to the ACTUAL logged-in clinician and the ACTUAL open patient, so the
 * audit trail names the real human instead of `admin`.
 *
 * Criteria asserted:
 *   C1 - the minted token is bound to the clinician uuid (not admin / the service
 *        account) and the open patient uuid, carrying ONLY patient/* resource
 *        scopes plus a launch context scope (no user/* leaks in).
 *   C2 - patient-scoping holds in BOTH directions at the FHIR transport seam: a
 *        read of the bound patient succeeds, a Patient search returns only the
 *        bound patient, and a cross-patient read is refused.
 *   C3 - after a real audit turn presenting the minted token, T016's decision-log
 *        AND §164.528 disclosure rows name the real clinician, not `admin`.
 *   C4 - the ServiceTokenProvider seam widened by exactly the patient-uuid
 *        argument, and the new provider implements it.
 *   C5 - a user who cannot satisfy the bridge guardrails (fails patients/demo ACL,
 *        or role != users) fails CLOSED: no droppable token is minted.
 *
 * NOTE: the token's patient-scoping is a correctness measure, not an authorization
 * boundary (OpenEMR has no per-patient ACL; the relay's SessionPatientAccessGuard
 * remains the gate). These assertions verify scoping/attribution, not access
 * control.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Api;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Modules\ClinicalCopilot\AgentUnavailableException;
use OpenEMR\Modules\ClinicalCopilot\ServiceTokenProvider;
use OpenEMR\Modules\ClinicalCopilot\SmartLaunchTokenProvider;
use PHPUnit\Framework\TestCase;
use ReflectionMethod;
use ReflectionNamedType;

class ClinicalCopilotSmartLaunchTokenApiTest extends TestCase
{
    private const MODULE_DIRECTORY = 'oe-module-clinical-copilot';
    private const EVENT_TYPE = 'ai-clinical-summary';

    /** Marker name the T027 launch/patient OAuth client is registered under. */
    private const SMART_CLIENT_NAME = 'oe-module-clinical-copilot smart-launch service';

    /** Stable dev-seed clinician (id=5, role=users, passes patients/demo) — NOT admin. */
    private const CLINICIAN_USERNAME = 'clinician';
    /** Stable dev-seed user that fails the patients/demo ACL guardrail. */
    private const ACL_FAIL_USERNAME = 'phimail-service';
    /** Stable dev-seed user whose role is NOT `users` (fails the role guardrail). */
    private const ROLE_FAIL_USERNAME = 'oe-system';

    /** The 8 FHIR resource types the agent's tools need, patient-scoped. */
    private const REQUIRED_RESOURCES = [
        'Patient', 'Observation', 'Condition', 'AllergyIntolerance',
        'MedicationRequest', 'Encounter', 'DocumentReference', 'Immunization',
    ];

    private int $clinicianId = 0;
    private string $clinicianUuid = '';
    private string $adminUuid = '';
    private string $boundPatientUuid = '';
    private int $boundPatientPid = 0;
    private string $otherPatientUuid = '';
    private ?int $originalModActive = null;

    protected function setUp(): void
    {
        $this->clinicianId = $this->requireUserId(self::CLINICIAN_USERNAME);
        $this->clinicianUuid = $this->uuidForUserId($this->clinicianId);
        $this->adminUuid = $this->uuidForUserId($this->requireUserId('admin'));

        // Bound (open) patient + a DIFFERENT patient for the cross-patient probe.
        $bound = QueryUtils::querySingleRow(
            'SELECT `pid`, `uuid` FROM `patient_data` WHERE `uuid` IS NOT NULL ORDER BY `pid` ASC LIMIT 1'
        );
        if (!is_array($bound) || !isset($bound['pid'], $bound['uuid'])) {
            $this->fail('Expected at least one patient with a uuid on the live stack.');
        }
        $this->boundPatientPid = (int) $bound['pid'];
        $this->boundPatientUuid = UuidRegistry::uuidToString($bound['uuid']);

        $other = QueryUtils::querySingleRow(
            'SELECT `uuid` FROM `patient_data` WHERE `uuid` IS NOT NULL AND `pid` <> ? ORDER BY `pid` ASC LIMIT 1',
            [$this->boundPatientPid]
        );
        if (!is_array($other) || !isset($other['uuid'])) {
            $this->fail('Expected a second patient with a uuid for the cross-patient probe.');
        }
        $this->otherPatientUuid = UuidRegistry::uuidToString($other['uuid']);

        // The audit-bridge endpoint (C3) is core-gated on the module being active.
        $modRow = QueryUtils::querySingleRow(
            'SELECT `mod_active` FROM `modules` WHERE `mod_directory` = ?',
            [self::MODULE_DIRECTORY]
        );
        $this->originalModActive = (is_array($modRow) && isset($modRow['mod_active']))
            ? (int) $modRow['mod_active']
            : null;
        $this->setModuleActive(1);
    }

    protected function tearDown(): void
    {
        // Scope-agnostic cleanup: delete every artifact keyed on the dedicated
        // client and the acting users, NOT on the exact scope JSON.
        $clientRows = QueryUtils::fetchRecords(
            'SELECT `client_id` FROM `oauth_clients` WHERE `client_name` = ?',
            [self::SMART_CLIENT_NAME]
        );
        foreach ($clientRows as $row) {
            $clientId = is_array($row) ? ($row['client_id'] ?? null) : null;
            if (!is_string($clientId) || $clientId === '') {
                continue;
            }
            QueryUtils::sqlStatementThrowException('DELETE FROM `api_token` WHERE `client_id` = ?', [$clientId]);
            QueryUtils::sqlStatementThrowException(
                "DELETE FROM `oauth_trusted_user` WHERE `client_id` = ? AND `grant_type` = 'password_grant'",
                [$clientId]
            );
            QueryUtils::sqlStatementThrowException('DELETE FROM `oauth_clients` WHERE `client_id` = ?', [$clientId]);
        }

        if ($this->boundPatientPid > 0) {
            QueryUtils::sqlStatementThrowException(
                'DELETE FROM `log` WHERE `event` = ? AND `patient_id` = ?',
                [self::EVENT_TYPE, $this->boundPatientPid]
            );
            QueryUtils::sqlStatementThrowException(
                'DELETE FROM `extended_log` WHERE `event` = ? AND `patient_id` = ?',
                [self::EVENT_TYPE, $this->boundPatientPid]
            );
        }

        if ($this->originalModActive === null) {
            QueryUtils::sqlStatementThrowException(
                'DELETE FROM `modules` WHERE `mod_directory` = ?',
                [self::MODULE_DIRECTORY]
            );
        } else {
            $this->setModuleActive($this->originalModActive);
        }
    }

    // ---------------------------------------------------------------- helpers

    private function requireUserId(string $username): int
    {
        $row = QueryUtils::querySingleRow('SELECT `id` FROM `users` WHERE `username` = ?', [$username]);
        if (!is_array($row) || !isset($row['id'])) {
            $this->fail("Expected dev-seed user '{$username}' to exist on the live stack.");
        }
        return (int) $row['id'];
    }

    private function uuidForUserId(int $id): string
    {
        $uuid = QueryUtils::fetchSingleValue('SELECT `uuid` FROM `users` WHERE `id` = ?', 'uuid', [$id]);
        if ($uuid === null || $uuid === '') {
            $this->fail("Expected user id={$id} to have a uuid.");
        }
        return UuidRegistry::uuidToString($uuid);
    }

    private function setModuleActive(int $active): void
    {
        $existing = QueryUtils::querySingleRow(
            'SELECT `mod_id` FROM `modules` WHERE `mod_directory` = ?',
            [self::MODULE_DIRECTORY]
        );
        if (!is_array($existing)) {
            QueryUtils::sqlStatementThrowException(
                'INSERT INTO `modules` '
                . '(`mod_name`, `mod_directory`, `mod_active`, `mod_ui_active`, `type`, '
                . '`directory`, `date`, `sql_run`, `sql_version`, `acl_version`) '
                . 'VALUES (?, ?, ?, 0, 0, \'\', NOW(), 1, \'0\', \'\')',
                ['Clinical Co-Pilot', self::MODULE_DIRECTORY, $active]
            );
            return;
        }
        QueryUtils::sqlStatementThrowException(
            'UPDATE `modules` SET `mod_active` = ? WHERE `mod_directory` = ?',
            [$active, self::MODULE_DIRECTORY]
        );
    }

    private function apiBaseUrl(): string
    {
        $base = getenv('OPENEMR_BASE_URL_API');
        return is_string($base) && $base !== '' ? rtrim($base, '/') : 'https://localhost';
    }

    /**
     * @return array{status: int, body: string}
     */
    private function httpGet(string $path, string $bearer): array
    {
        $ch = curl_init($this->apiBaseUrl() . $path);
        curl_setopt_array($ch, [
            CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $bearer, 'Accept: application/json'],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_SSL_VERIFYPEER => false,
            CURLOPT_SSL_VERIFYHOST => false,
            CURLOPT_TIMEOUT => 25,
        ]);
        $raw = curl_exec($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        return ['status' => $status, 'body' => is_string($raw) ? $raw : ''];
    }

    /**
     * @return array{status: int, body: string}
     */
    private function httpPostJson(string $path, string $bearer, string $json): array
    {
        $ch = curl_init($this->apiBaseUrl() . $path);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $json,
            CURLOPT_HTTPHEADER => [
                'Authorization: Bearer ' . $bearer,
                'Content-Type: application/json',
                'Accept: application/json',
            ],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_SSL_VERIFYPEER => false,
            CURLOPT_SSL_VERIFYHOST => false,
            CURLOPT_TIMEOUT => 25,
        ]);
        $raw = curl_exec($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        return ['status' => $status, 'body' => is_string($raw) ? $raw : ''];
    }

    /**
     * @return array<string, mixed>
     */
    private function decodeJwtPayload(string $jwt): array
    {
        $parts = explode('.', $jwt);
        $this->assertCount(3, $parts, 'Minted token must be a signed JWT (three dot-separated segments).');
        $decoded = json_decode((string) base64_decode(strtr($parts[1], '-_', '+/')), true);
        $this->assertIsArray($decoded, 'JWT payload must decode to an array.');
        /** @var array<string, mixed> $decoded */
        return $decoded;
    }

    /**
     * @return array{user_id: string, scope: array<int, string>, context: array<string, mixed>}
     */
    private function persistedTokenRow(): array
    {
        $clientId = QueryUtils::fetchSingleValue(
            'SELECT `client_id` FROM `oauth_clients` WHERE `client_name` = ? AND `is_enabled` = 1 '
            . 'ORDER BY `register_date` DESC LIMIT 1',
            'client_id',
            [self::SMART_CLIENT_NAME]
        );
        $this->assertIsString($clientId, 'A dedicated enabled launch/patient client must have been provisioned.');

        $row = QueryUtils::querySingleRow(
            'SELECT `user_id`, `scope`, `context` FROM `api_token` WHERE `client_id` = ? AND `user_id` = ? '
            . 'ORDER BY `id` DESC LIMIT 1',
            [$clientId, $this->clinicianUuid]
        );
        $this->assertIsArray($row, 'The minted access token must have been persisted to api_token.');

        $scope = json_decode((string) $row['scope'], true);
        $this->assertIsArray($scope, 'Persisted scope must decode to a list.');
        $context = json_decode((string) $row['context'], true);
        $this->assertIsArray($context, 'Persisted context must decode to a map.');

        /** @var array<int, string> $scope */
        /** @var array<string, mixed> $context */
        return [
            'user_id' => (string) $row['user_id'],
            'scope' => array_values(array_map('strval', $scope)),
            'context' => $context,
        ];
    }

    // ---------------------------------------------------------------------- C1

    public function testMintedTokenBindsClinicianAndPatientWithPatientOnlyScopes(): void
    {
        $provider = new SmartLaunchTokenProvider($this->clinicianId);
        $jwt = $provider->getToken($this->boundPatientUuid);
        $this->assertNotSame('', $jwt, 'Provider must return a non-empty access token.');

        $row = $this->persistedTokenRow();

        // Attributed to the real clinician, NOT admin / the service account.
        $this->assertSame($this->clinicianUuid, $row['user_id'], 'Token must be bound to the clinician uuid.');
        $this->assertNotSame($this->adminUuid, $row['user_id'], 'Token must NOT be bound to the admin/service account.');

        // The JWT subject agrees.
        $payload = $this->decodeJwtPayload($jwt);
        $this->assertSame($this->clinicianUuid, $payload['sub'] ?? null, 'JWT sub must be the clinician uuid.');

        // Bound to the open patient via SMART launch context.
        $this->assertSame(
            $this->boundPatientUuid,
            $row['context']['patient'] ?? null,
            'Token context.patient must equal the open patient uuid.'
        );

        // Every one of the 8 resource types is present, patient-scoped, plus launch.
        foreach (self::REQUIRED_RESOURCES as $resource) {
            $this->assertContains(
                'patient/' . $resource . '.read',
                $row['scope'],
                "Token must carry patient/{$resource}.read."
            );
        }
        $this->assertContains('launch', $row['scope'], 'Token must carry a launch context scope.');

        // THE trap (both directions): no user/* scope may leak in — a stray one
        // silently widens reads to all patients.
        foreach ($row['scope'] as $scope) {
            $this->assertStringStartsNotWith(
                'user/',
                $scope,
                'No user/* scope may be present (it defeats patient scoping): ' . $scope
            );
        }
    }

    // ---------------------------------------------------------------------- C2

    public function testPatientScopingHoldsInBothDirectionsAtFhirSeam(): void
    {
        $provider = new SmartLaunchTokenProvider($this->clinicianId);
        $jwt = $provider->getToken($this->boundPatientUuid);

        // Bound patient read succeeds.
        $bound = $this->httpGet('/apis/default/fhir/Patient/' . $this->boundPatientUuid, $jwt);
        $this->assertSame(200, $bound['status'], 'A read of the bound patient must succeed. Body: ' . $bound['body']);
        $this->assertStringContainsString(
            $this->boundPatientUuid,
            $bound['body'],
            'The bound-patient read must return the bound patient resource.'
        );

        // Patient search returns ONLY the bound patient.
        $search = $this->httpGet('/apis/default/fhir/Patient', $jwt);
        $this->assertSame(200, $search['status'], 'Patient search must succeed. Body: ' . $search['body']);
        $bundle = json_decode($search['body'], true);
        $this->assertIsArray($bundle, 'Search must return a FHIR Bundle.');
        $this->assertSame(1, $bundle['total'] ?? null, 'Search must return exactly the one bound patient (total=1).');
        $this->assertStringContainsString($this->boundPatientUuid, $search['body'], 'Bundle must contain the bound patient.');
        $this->assertStringNotContainsString(
            $this->otherPatientUuid,
            $search['body'],
            'Bundle must NOT contain any other patient — a user/* scope leak would widen this to all patients.'
        );

        // Cross-patient read is refused (not a 200 with the other patient's data).
        $cross = $this->httpGet('/apis/default/fhir/Patient/' . $this->otherPatientUuid, $jwt);
        $this->assertNotSame(
            200,
            $cross['status'],
            'A read of a DIFFERENT patient must be refused, not served. Body: ' . $cross['body']
        );
    }

    // ---------------------------------------------------------------------- C3

    public function testAuditRowsNameTheRealClinicianNotAdmin(): void
    {
        $provider = new SmartLaunchTokenProvider($this->clinicianId);
        $jwt = $provider->getToken($this->boundPatientUuid);

        $record = json_encode([
            'user_token_hash' => hash('sha256', $jwt),
            'patient_id' => $this->boundPatientUuid,
            'correlation_id' => 'corr-t027-' . bin2hex(random_bytes(6)),
            'conversation_id' => 'conv-t027-' . bin2hex(random_bytes(6)),
            'occurred_at' => gmdate('Y-m-d\TH:i:s\Z'),
            'claims_total' => 3,
            'claims_passed' => 3,
            'claims_stripped' => 0,
            'outcome' => 'answered',
            'degraded' => null,
            'fallback_reason' => null,
        ]);
        $this->assertIsString($record);

        $response = $this->httpPostJson(
            '/interface/modules/custom_modules/oe-module-clinical-copilot/public/audit-bridge.php',
            $jwt,
            $record
        );
        $this->assertSame(
            201,
            $response['status'],
            'A valid audit turn with the minted token must be recorded (201). Body: ' . $response['body']
        );

        // The decision-log row names the real clinician, not admin / the service user.
        $logRow = QueryUtils::querySingleRow(
            'SELECT `user` FROM `log` WHERE `event` = ? AND `patient_id` = ? ORDER BY `id` DESC LIMIT 1',
            [self::EVENT_TYPE, $this->boundPatientPid]
        );
        $this->assertIsArray($logRow, 'A decision-log row must have been written.');
        $this->assertSame(self::CLINICIAN_USERNAME, $logRow['user'], 'log.user must be the real clinician.');
        $this->assertNotSame('admin', $logRow['user'], 'log.user must NOT be the service account.');

        // The §164.528 disclosure row likewise names the clinician.
        $discRow = QueryUtils::querySingleRow(
            'SELECT `user` FROM `extended_log` WHERE `event` = ? AND `patient_id` = ? ORDER BY `id` DESC LIMIT 1',
            [self::EVENT_TYPE, $this->boundPatientPid]
        );
        $this->assertIsArray($discRow, 'A disclosure row must have been written.');
        $this->assertSame(self::CLINICIAN_USERNAME, $discRow['user'], 'extended_log.user must be the real clinician.');
        $this->assertNotSame('admin', $discRow['user'], 'extended_log.user must NOT be the service account.');
    }

    // ---------------------------------------------------------------------- C4

    public function testServiceTokenProviderSeamWidenedByPatientUuidArgument(): void
    {
        $method = new ReflectionMethod(ServiceTokenProvider::class, 'getToken');
        $this->assertSame(1, $method->getNumberOfParameters(), 'getToken must take exactly one argument (the patient uuid).');
        $this->assertSame(1, $method->getNumberOfRequiredParameters(), 'The patient-uuid argument must be required.');

        $param = $method->getParameters()[0];
        $type = $param->getType();
        $this->assertInstanceOf(ReflectionNamedType::class, $type, 'The patient-uuid argument must be typed.');
        $this->assertSame('string', $type->getName(), 'The patient-uuid argument must be a string.');

        $this->assertTrue(
            is_subclass_of(SmartLaunchTokenProvider::class, ServiceTokenProvider::class),
            'SmartLaunchTokenProvider must implement the widened ServiceTokenProvider seam.'
        );
    }

    // ---------------------------------------------------------------------- C5

    public function testGuardrailFailingUsersFailClosedWithoutMintingADroppableToken(): void
    {
        // (a) A user who fails the patients/demo ACL — the bridge would drop its token.
        $aclFailId = $this->requireUserId(self::ACL_FAIL_USERNAME);
        $aclFailUuid = $this->uuidForUserId($aclFailId);
        $beforeAcl = $this->tokenCountForUser($aclFailUuid);

        $aclProvider = new SmartLaunchTokenProvider($aclFailId);
        try {
            $aclProvider->getToken($this->boundPatientUuid);
            $this->fail('A user failing the patients/demo ACL must not receive a token.');
        } catch (AgentUnavailableException) {
            // expected — fails closed
        }
        $this->assertSame(
            $beforeAcl,
            $this->tokenCountForUser($aclFailUuid),
            'No token may be minted for an ACL-failing user (it would be silently dropped by the bridge).'
        );

        // (b) A user whose role is not `users` — the bridge rejects the token.
        $roleFailId = $this->requireUserId(self::ROLE_FAIL_USERNAME);
        $roleFailUuid = $this->uuidForUserId($roleFailId);
        $beforeRole = $this->tokenCountForUser($roleFailUuid);

        $roleProvider = new SmartLaunchTokenProvider($roleFailId);
        try {
            $roleProvider->getToken($this->boundPatientUuid);
            $this->fail('A user whose role is not `users` must not receive a token.');
        } catch (AgentUnavailableException) {
            // expected — fails closed
        }
        $this->assertSame(
            $beforeRole,
            $this->tokenCountForUser($roleFailUuid),
            'No token may be minted for a non-users-role user.'
        );
    }

    private function tokenCountForUser(string $userUuid): int
    {
        return count(QueryUtils::fetchRecords(
            'SELECT `id` FROM `api_token` WHERE `user_id` = ?',
            [$userUuid]
        ));
    }
}
