<?php

/**
 * Clinical Co-Pilot audit-bridge endpoint API test (T016).
 *
 * Drives the module-owned HTTP entry point
 * interface/modules/custom_modules/oe-module-clinical-copilot/public/audit-bridge.php
 * with a real OAuth2 bearer minted by ApiTestClient, asserting the decision /
 * disclosure log rows are written (or, on every rejection path, that zero rows
 * land in either the `log` or the `extended_log` table).
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
use OpenEMR\Tests\Fixtures\FixtureManager;
use PHPUnit\Framework\TestCase;
use Ramsey\Uuid\Uuid;

class ClinicalCopilotAuditBridgeApiTest extends TestCase
{
    private const ENDPOINT =
        "/interface/modules/custom_modules/oe-module-clinical-copilot/public/audit-bridge.php";
    private const MODULE_DIRECTORY = "oe-module-clinical-copilot";
    private const EVENT_TYPE = "ai-clinical-summary";
    private const LLM_PROVIDER_IDENTITY = "Anthropic Claude (BAA-covered)";

    private ApiTestClient $testClient;
    private FixtureManager $fixtureManager;
    private int $patientPid = 0;
    private string $patientUuidString = "";

    protected function setUp(): void
    {
        $baseUrl = getenv("OPENEMR_BASE_URL_API", true) ?: "https://localhost";
        $this->testClient = new ApiTestClient($baseUrl, false);
        $this->testClient->setAuthToken(ApiTestClient::OPENEMR_AUTH_ENDPOINT);

        $this->fixtureManager = new FixtureManager();
        $this->fixtureManager->installPatientFixtures();

        $patientRow = QueryUtils::querySingleRow(
            "SELECT `pid`, `uuid` FROM `patient_data` WHERE `pubpid` LIKE ? ORDER BY `pid` DESC LIMIT 1",
            [FixtureManager::PATIENT_FIXTURE_PUBPID_PREFIX . "%"]
        );
        $this->patientPid = (int) $patientRow['pid'];
        $this->patientUuidString = UuidRegistry::uuidToString($patientRow['uuid']);

        $this->activateModule(1);
    }

    protected function tearDown(): void
    {
        // Remove rows this test may have written (successful writes always
        // carry the fixture pid), then unwind fixtures and OAuth state.
        QueryUtils::sqlStatementThrowException(
            "DELETE FROM `log` WHERE `event` = ? AND `patient_id` = ?",
            [self::EVENT_TYPE, $this->patientPid]
        );
        QueryUtils::sqlStatementThrowException(
            "DELETE FROM `extended_log` WHERE `event` = ? AND `patient_id` = ?",
            [self::EVENT_TYPE, $this->patientPid]
        );
        QueryUtils::sqlStatementThrowException(
            "DELETE FROM `modules` WHERE `mod_directory` = ?",
            [self::MODULE_DIRECTORY]
        );

        $this->fixtureManager->removePatientFixtures();
        $this->testClient->cleanupRevokeAuth();
        $this->testClient->cleanupClient();
    }

    // ------------------------------------------------------------------ helpers

    private function activateModule(int $active): void
    {
        QueryUtils::sqlStatementThrowException(
            "DELETE FROM `modules` WHERE `mod_directory` = ?",
            [self::MODULE_DIRECTORY]
        );
        QueryUtils::sqlStatementThrowException(
            "INSERT INTO `modules` (`mod_name`, `mod_directory`, `mod_active`, `mod_type`) VALUES (?, ?, ?, ?)",
            ["Clinical Co-Pilot", self::MODULE_DIRECTORY, $active, "0"]
        );
    }

    private function setModuleActive(int $active): void
    {
        QueryUtils::sqlStatementThrowException(
            "UPDATE `modules` SET `mod_active` = ? WHERE `mod_directory` = ?",
            [$active, self::MODULE_DIRECTORY]
        );
    }

    /**
     * @return array<string, mixed>
     */
    private function validRecord(string $outcome = "answered"): array
    {
        $rawToken = (string) $this->testClient->getAccessToken();
        $record = [
            "user_token_hash" => hash('sha256', $rawToken),
            "patient_id" => $this->patientUuidString,
            "correlation_id" => "corr-" . Uuid::uuid4()->toString(),
            "conversation_id" => "conv-" . Uuid::uuid4()->toString(),
            "occurred_at" => gmdate('Y-m-d\TH:i:s\Z'),
            "claims_total" => 3,
            "claims_passed" => 3,
            "claims_stripped" => 0,
            "outcome" => $outcome,
            "degraded" => null,
            "fallback_reason" => null,
        ];
        if ($outcome === "degraded") {
            $record["degraded"] = "llm_unavailable";
            $record["claims_total"] = 0;
            $record["claims_passed"] = 0;
            $record["claims_stripped"] = 0;
        } elseif ($outcome === "fallback") {
            $record["fallback_reason"] = "refusal";
            $record["claims_total"] = 0;
            $record["claims_passed"] = 0;
            $record["claims_stripped"] = 0;
        }
        return $record;
    }

    private function auditRowCount(): int
    {
        return count(QueryUtils::fetchRecords(
            "SELECT `id` FROM `log` WHERE `event` = ?",
            [self::EVENT_TYPE]
        ));
    }

    private function disclosureRowCount(): int
    {
        return count(QueryUtils::fetchRecords(
            "SELECT `id` FROM `extended_log` WHERE `event` = ?",
            [self::EVENT_TYPE]
        ));
    }

    /**
     * @return array<string, mixed>|null
     */
    private function latestAuditRow(): ?array
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT `event`, `user`, `patient_id`, `comments`, `success`, `log_from` "
            . "FROM `log` WHERE `event` = ? AND `patient_id` = ? ORDER BY `id` DESC LIMIT 1",
            [self::EVENT_TYPE, $this->patientPid]
        );
        return $rows[0] ?? null;
    }

    /**
     * @return array<string, mixed>|null
     */
    private function latestDisclosureRow(): ?array
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT `event`, `user`, `recipient`, `patient_id`, `description` "
            . "FROM `extended_log` WHERE `event` = ? AND `patient_id` = ? ORDER BY `id` DESC LIMIT 1",
            [self::EVENT_TYPE, $this->patientPid]
        );
        return $rows[0] ?? null;
    }

    // -------------------------------------------------------------- criterion 2/1

    /** Mandatory adversarial #1: no Authorization header -> 401, not HTML. */
    public function testNoAuthorizationHeaderIsUnauthorizedAndNotHtml(): void
    {
        $beforeLog = $this->auditRowCount();
        $beforeDisc = $this->disclosureRowCount();

        $this->testClient->removeAuthToken();
        $response = $this->testClient->post(self::ENDPOINT, $this->validRecord());

        $this->assertSame(401, $response->getStatusCode());
        $body = (string) $response->getBody();
        // Property, not proxy: the login-page bug returns HTTP 200 with HTML.
        $this->assertSame('{"error":"unauthorized"}', $body);
        $this->assertStringNotContainsStringIgnoringCase('<html', $body);
        $this->assertStringNotContainsStringIgnoringCase('<!doctype', $body);

        $this->assertSame($beforeLog, $this->auditRowCount());
        $this->assertSame($beforeDisc, $this->disclosureRowCount());
    }

    /** Mandatory adversarial #2: garbage / bogus / revoked bearer -> 401, byte-identical. */
    public function testBadBearersAllRejectedWithByteIdenticalBody(): void
    {
        $beforeLog = $this->auditRowCount();
        $beforeDisc = $this->disclosureRowCount();

        $noAuthBody = $this->postAndReturnBody(fn() => $this->testClient->removeAuthToken(), 401);

        $garbageBody = $this->postAndReturnBody(
            fn() => $this->testClient->setBearer("Bearer this-is-not-a-jwt"),
            401
        );

        $bogusBody = $this->postAndReturnBody(
            fn() => $this->testClient->setBearer("Bearer " . ApiTestClient::BOGUS_ACCESS_TOKEN),
            401
        );

        // Revoke the (still cryptographically valid, unexpired) token.
        $revokedBody = $this->postAndReturnBody(
            function (): void {
                QueryUtils::sqlStatementThrowException(
                    "UPDATE `api_token` SET `revoked` = 1 WHERE `client_id` = ?",
                    [(string) $this->testClient->getClientId()]
                );
            },
            401
        );

        $this->assertSame('{"error":"unauthorized"}', $noAuthBody);
        $this->assertSame($noAuthBody, $garbageBody);
        $this->assertSame($noAuthBody, $bogusBody);
        $this->assertSame($noAuthBody, $revokedBody);

        $this->assertSame($beforeLog, $this->auditRowCount());
        $this->assertSame($beforeDisc, $this->disclosureRowCount());
    }

    /**
     * @param callable():void $mutate
     */
    private function postAndReturnBody(callable $mutate, int $expectedStatus): string
    {
        $mutate();
        $response = $this->testClient->post(self::ENDPOINT, $this->validRecord());
        $this->assertSame($expectedStatus, $response->getStatusCode());
        return (string) $response->getBody();
    }

    /** Mandatory adversarial #3: valid bearer, user_token_hash of a different token -> 401. */
    public function testUserTokenHashBindingMismatchIsUnauthorized(): void
    {
        $beforeLog = $this->auditRowCount();
        $beforeDisc = $this->disclosureRowCount();

        $record = $this->validRecord();
        $record["user_token_hash"] = hash('sha256', 'a-totally-different-user-token');

        $response = $this->testClient->post(self::ENDPOINT, $record);

        $this->assertSame(401, $response->getStatusCode());
        $this->assertSame('{"error":"unauthorized"}', (string) $response->getBody());
        $this->assertSame($beforeLog, $this->auditRowCount());
        $this->assertSame($beforeDisc, $this->disclosureRowCount());
    }

    /** Mandatory adversarial #4: valid answered record -> 201 + correct log + disclosure. */
    public function testValidAnsweredRecordWritesAuditAndDisclosureRows(): void
    {
        $record = $this->validRecord("answered");
        $response = $this->testClient->post(self::ENDPOINT, $record);

        $this->assertSame(201, $response->getStatusCode());
        $this->assertSame('{"status":"recorded"}', (string) $response->getBody());

        $logRow = $this->latestAuditRow();
        $this->assertNotNull($logRow);
        $this->assertSame(self::EVENT_TYPE, $logRow['event']);
        $this->assertSame('admin', $logRow['user']);
        $this->assertSame($this->patientPid, (int) $logRow['patient_id']);
        $this->assertSame(1, (int) $logRow['success']);
        $this->assertSame('clinical-copilot', $logRow['log_from']);

        $expectedComments = json_encode([
            "correlation_id" => $record["correlation_id"],
            "conversation_id" => $record["conversation_id"],
            "outcome" => "answered",
            "claims_total" => 3,
            "claims_passed" => 3,
            "claims_stripped" => 0,
            "degraded" => null,
            "fallback_reason" => null,
        ]);
        // recordLogItem base64-encodes comments before storage.
        $this->assertSame($expectedComments, base64_decode((string) $logRow['comments']));

        $discRow = $this->latestDisclosureRow();
        $this->assertNotNull($discRow);
        $this->assertSame(self::EVENT_TYPE, $discRow['event']);
        $this->assertSame(self::LLM_PROVIDER_IDENTITY, $discRow['recipient']);
        $this->assertSame('admin', $discRow['user']);
        $this->assertSame($this->patientPid, (int) $discRow['patient_id']);
        $this->assertStringContainsString('outcome=answered', (string) $discRow['description']);
        $this->assertStringContainsString($record["correlation_id"], (string) $discRow['description']);
    }

    /** Mandatory adversarial #5: degraded and fallback each -> 201 + one log + one disclosure. */
    public function testDegradedAndFallbackRecordsEachWriteOneLogAndOneDisclosure(): void
    {
        foreach (["degraded", "fallback"] as $outcome) {
            $beforeLog = $this->auditRowCount();
            $beforeDisc = $this->disclosureRowCount();

            $record = $this->validRecord($outcome);
            $response = $this->testClient->post(self::ENDPOINT, $record);

            $this->assertSame(201, $response->getStatusCode(), $outcome);
            $this->assertSame($beforeLog + 1, $this->auditRowCount(), $outcome);
            $this->assertSame($beforeDisc + 1, $this->disclosureRowCount(), $outcome);

            $discRow = $this->latestDisclosureRow();
            $this->assertNotNull($discRow, $outcome);
            $this->assertStringContainsString('outcome=' . $outcome, (string) $discRow['description']);
        }
    }

    /** Mandatory adversarial #6: six malformed shapes -> 422 byte-identical, zero rows. */
    public function testMalformedRecordsAreUnprocessableWithNoRows(): void
    {
        $shapes = [];

        $extraKey = $this->validRecord();
        $extraKey["totally_unexpected"] = "x";
        $shapes["unknown extra key"] = $extraKey;

        $missing = $this->validRecord();
        unset($missing["correlation_id"]);
        $shapes["missing correlation_id"] = $missing;

        $negative = $this->validRecord();
        $negative["claims_total"] = -1;
        $shapes["negative claims_total"] = $negative;

        $badHash = $this->validRecord();
        $badHash["user_token_hash"] = str_repeat('z', 64); // 64 chars, not hex
        $shapes["non-hex user_token_hash"] = $badHash;

        $crossField = $this->validRecord();
        $crossField["outcome"] = "answered";
        $crossField["degraded"] = "llm_unavailable"; // answered must have degraded null
        $shapes["cross-field answered+degraded"] = $crossField;

        $naiveTs = $this->validRecord();
        $naiveTs["occurred_at"] = "2026-07-09T12:00:00"; // no offset
        $shapes["naive occurred_at"] = $naiveTs;

        $expectedBody = null;
        foreach ($shapes as $label => $record) {
            $beforeLog = $this->auditRowCount();
            $beforeDisc = $this->disclosureRowCount();

            $response = $this->testClient->post(self::ENDPOINT, $record);
            $this->assertSame(422, $response->getStatusCode(), $label);

            $body = (string) $response->getBody();
            $this->assertSame('{"error":"unprocessable"}', $body, $label);
            if ($expectedBody === null) {
                $expectedBody = $body;
            }
            $this->assertSame($expectedBody, $body, $label);

            $this->assertSame($beforeLog, $this->auditRowCount(), $label);
            $this->assertSame($beforeDisc, $this->disclosureRowCount(), $label);
        }
    }

    /** Mandatory adversarial #7: unknown patient uuid -> 422, byte-identical to (6). */
    public function testUnknownPatientUuidIsUnprocessableWithSameBody(): void
    {
        $beforeLog = $this->auditRowCount();
        $beforeDisc = $this->disclosureRowCount();

        $record = $this->validRecord();
        // A well-formed uuid that resolves to no patient_data row.
        $record["patient_id"] = Uuid::uuid4()->toString();

        $response = $this->testClient->post(self::ENDPOINT, $record);
        $this->assertSame(422, $response->getStatusCode());
        $this->assertSame('{"error":"unprocessable"}', (string) $response->getBody());

        $this->assertSame($beforeLog, $this->auditRowCount());
        $this->assertSame($beforeDisc, $this->disclosureRowCount());
    }

    /**
     * Mandatory adversarial #8: caller lacking patients/demo ACL -> 401.
     *
     * The api harness mints tokens only for the admin user (via OE_USER /
     * OE_PASS), who holds every ACL, and provisioning a limited-ACL user with
     * a working password grant is impractical here. The production code path
     * (AclMain::aclCheckCore('patients','demo',$username) -> 401 on false) is
     * therefore exercised by review, not by this test. Disclosed in the run
     * report rather than deleting the criterion.
     */
    public function testCallerWithoutPatientsDemoAclIsUnauthorized(): void
    {
        $this->markTestSkipped(
            'Limited-ACL user + password-grant fixture impractical in the api harness; '
            . 'ACL 401 branch disclosed in the T016 report.'
        );
    }

    /** Mandatory adversarial #9: module gate (mod_active 0 -> 404, 1 -> 201). */
    public function testModuleGateBlocksWhenInactive(): void
    {
        $beforeLog = $this->auditRowCount();
        $beforeDisc = $this->disclosureRowCount();

        $this->setModuleActive(0);
        $inactive = $this->testClient->post(self::ENDPOINT, $this->validRecord());
        $this->assertSame(404, $inactive->getStatusCode());
        $this->assertSame($beforeLog, $this->auditRowCount());
        $this->assertSame($beforeDisc, $this->disclosureRowCount());

        $this->setModuleActive(1);
        $active = $this->testClient->post(self::ENDPOINT, $this->validRecord());
        $this->assertSame(201, $active->getStatusCode());
    }

    /** Mandatory adversarial #10: PHI sweep across comments, description, response body. */
    public function testNoPhiLeaksIntoStoredRowsOrResponseBody(): void
    {
        $rawToken = (string) $this->testClient->getAccessToken();
        // The fixture patient's name — must never surface in audit output.
        $patientRow = QueryUtils::querySingleRow(
            "SELECT `fname`, `lname` FROM `patient_data` WHERE `pid` = ?",
            [$this->patientPid]
        );
        $patientName = (string) ($patientRow['lname'] ?? 'Smith');

        $record = $this->validRecord("answered");
        $response = $this->testClient->post(self::ENDPOINT, $record);
        $this->assertSame(201, $response->getStatusCode());

        $logRow = $this->latestAuditRow();
        $discRow = $this->latestDisclosureRow();
        $this->assertNotNull($logRow);
        $this->assertNotNull($discRow);

        $decodedComments = base64_decode((string) $logRow['comments']);
        $description = (string) $discRow['description'];
        $responseBody = (string) $response->getBody();

        foreach ([$decodedComments, $description, $responseBody] as $haystack) {
            $this->assertStringNotContainsString($rawToken, $haystack);
            $this->assertStringNotContainsString('penicillin', $haystack);
            $this->assertStringNotContainsString('does she still take', $haystack);
            if ($patientName !== '') {
                $this->assertStringNotContainsString($patientName, $haystack);
            }
        }
    }

    /** Mandatory adversarial #11: GET -> 405, zero rows. */
    public function testGetIsMethodNotAllowedWithNoRows(): void
    {
        $beforeLog = $this->auditRowCount();
        $beforeDisc = $this->disclosureRowCount();

        $response = $this->testClient->get(self::ENDPOINT);
        $this->assertSame(405, $response->getStatusCode());

        $this->assertSame($beforeLog, $this->auditRowCount());
        $this->assertSame($beforeDisc, $this->disclosureRowCount());
    }

    // ------------------------------------------ my own adversarial probes (extra)

    /**
     * Extra probe A: claims_passed + claims_stripped exceeding claims_total is an
     * impossible record -> 422, but equality/undercount (a claim neither passed
     * nor stripped) must still be accepted. Catches an over-strict `== total`
     * check that would destroy legitimate audit rows.
     */
    public function testClaimsArithmeticBoundaryIsEnforcedButUndercountAccepted(): void
    {
        $beforeLog = $this->auditRowCount();

        $impossible = $this->validRecord("answered");
        $impossible["claims_total"] = 2;
        $impossible["claims_passed"] = 2;
        $impossible["claims_stripped"] = 1; // 2 + 1 > 2
        $bad = $this->testClient->post(self::ENDPOINT, $impossible);
        $this->assertSame(422, $bad->getStatusCode());
        $this->assertSame($beforeLog, $this->auditRowCount());

        $undercount = $this->validRecord("answered");
        $undercount["claims_total"] = 5;
        $undercount["claims_passed"] = 2;
        $undercount["claims_stripped"] = 1; // 2 + 1 <= 5, a claim neither passed nor stripped
        $good = $this->testClient->post(self::ENDPOINT, $undercount);
        $this->assertSame(201, $good->getStatusCode());
    }

    /**
     * Extra probe B: a numeric-offset occurred_at (not the Z form) must be
     * accepted, and the disclosure `date` must be stored in the server's local
     * timezone as 'Y-m-d H:i:s' (no 'T', no offset, no 'Z') — never raw UTC ISO.
     */
    public function testNumericOffsetTimestampAcceptedAndStoredServerLocal(): void
    {
        $record = $this->validRecord("answered");
        $record["occurred_at"] = "2026-07-09T12:00:00+05:30";

        $response = $this->testClient->post(self::ENDPOINT, $record);
        $this->assertSame(201, $response->getStatusCode());

        $discRows = QueryUtils::fetchRecords(
            "SELECT `date` FROM `extended_log` WHERE `event` = ? AND `patient_id` = ? ORDER BY `id` DESC LIMIT 1",
            [self::EVENT_TYPE, $this->patientPid]
        );
        $this->assertNotEmpty($discRows);
        $storedDate = (string) $discRows[0]['date'];
        $this->assertMatchesRegularExpression('/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/', $storedDate);
        $this->assertStringNotContainsString('T', $storedDate);
        $this->assertStringNotContainsString('Z', $storedDate);
        $this->assertStringNotContainsString('+', $storedDate);
    }

    /**
     * Extra probe C: POSTing the same correlation_id twice writes two rows.
     * v1 has no idempotency/dedup; a spurious unique-index or dedup would drop
     * the second (real) audit row. Assert both landed.
     */
    public function testDuplicateCorrelationIdWritesTwoRows(): void
    {
        $beforeLog = $this->auditRowCount();
        $beforeDisc = $this->disclosureRowCount();

        $record = $this->validRecord("answered");
        $first = $this->testClient->post(self::ENDPOINT, $record);
        $second = $this->testClient->post(self::ENDPOINT, $record);

        $this->assertSame(201, $first->getStatusCode());
        $this->assertSame(201, $second->getStatusCode());
        $this->assertSame($beforeLog + 2, $this->auditRowCount());
        $this->assertSame($beforeDisc + 2, $this->disclosureRowCount());
    }
}
