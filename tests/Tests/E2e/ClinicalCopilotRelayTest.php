<?php

/**
 * Clinical Co-Pilot panel -> agent relay E2E test (T021).
 *
 * Drives a real logged-in browser session against the patient Dashboard and
 * exercises the module-owned, SESSION-authenticated relay endpoint end to end:
 *
 *   C1 - the relay accepts the panel's POST only inside the authenticated
 *        session and only with a valid CSRF token; an unauthenticated request,
 *        a missing / garbage CSRF token, and a patient the session has not
 *        opened are each rejected.
 *   C2 - a valid request passes CSRF+ACL and reaches the agent (the relay mints
 *        a service token server-side), and the browser-facing response never
 *        contains a bearer/JWT/service-token string.
 *   C3 - a successful agent reply renders as an assistant bubble
 *        (.oe-copilot__bubble--agent) in the message list, via textContent
 *        (HTML in the reply is not parsed as markup). Because no LLM key is
 *        available in CI the agent itself cannot answer, so the RENDER path is
 *        driven against a stubbed relay response in the browser while the REAL
 *        relay/token/agent path is exercised separately (C2 above). Documented.
 *   C4 - when the relay cannot reach the agent, the panel shows a graceful
 *        "couldn't reach the assistant" state that leaks no exception text, and
 *        the request does not hang.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\E2e;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Tests\E2e\Base\BaseTrait;
use OpenEMR\Tests\E2e\Login\LoginTestData;
use OpenEMR\Tests\E2e\Login\LoginTrait;
use PHPUnit\Framework\Attributes\Test;
use Symfony\Component\Panther\PantherTestCase;

class ClinicalCopilotRelayTest extends PantherTestCase
{
    use BaseTrait;
    use LoginTrait;

    private $crawler;

    private const MODULE_DIRECTORY = 'oe-module-clinical-copilot';
    private const RELAY_PATH =
        '/interface/modules/custom_modules/oe-module-clinical-copilot/public/copilot-relay.php';

    /** Stable dev-fixture patient the panel is tested against (T019/T021). */
    private const FIXTURE_PID = 100;

    /** Marker the relay's provisioned service OAuth client is named with. */
    private const RELAY_CLIENT_NAME = 'oe-module-clinical-copilot relay service';

    private ?int $originalModActive = null;
    private string $openPatientUuid = '';
    private string $otherPatientUuid = '';

    protected function setUp(): void
    {
        parent::setUp();

        $openRow = QueryUtils::querySingleRow(
            'SELECT `uuid` FROM `patient_data` WHERE `pid` = ?',
            [self::FIXTURE_PID]
        );
        if (!is_array($openRow) || !isset($openRow['uuid'])) {
            $this->fail('Expected fixture patient pid=' . self::FIXTURE_PID . ' to exist.');
        }
        $this->openPatientUuid = UuidRegistry::uuidToString($openRow['uuid']);

        // A DIFFERENT existing patient, for the ACL "not the open patient" probe.
        $otherRow = QueryUtils::querySingleRow(
            'SELECT `uuid` FROM `patient_data` WHERE `pid` <> ? AND `uuid` IS NOT NULL '
            . 'AND `uuid` <> ? ORDER BY `pid` ASC LIMIT 1',
            [self::FIXTURE_PID, '']
        );
        if (is_array($otherRow) && isset($otherRow['uuid'])) {
            $this->otherPatientUuid = UuidRegistry::uuidToString($otherRow['uuid']);
        }

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
        $this->setModuleActive($this->originalModActive ?? 1);

        // Clean up any OAuth client the relay provisioned during this run.
        $clients = QueryUtils::fetchRecords(
            'SELECT `client_id` FROM `oauth_clients` WHERE `client_name` = ?',
            [self::RELAY_CLIENT_NAME]
        );
        foreach ($clients as $client) {
            $clientId = is_array($client) ? ($client['client_id'] ?? null) : null;
            if (is_string($clientId)) {
                QueryUtils::sqlStatementThrowException('DELETE FROM `api_token` WHERE `client_id` = ?', [$clientId]);
                QueryUtils::sqlStatementThrowException('DELETE FROM `oauth_clients` WHERE `client_id` = ?', [$clientId]);
            }
        }

        parent::tearDown();
    }

    /**
     * Upsert, not a blind UPDATE: a sibling suite's teardown (T016's
     * ClinicalCopilotAuditBridgeApiTest) unconditionally DELETEs this same
     * shared row, so a suite that only ever UPDATEs it can silently affect
     * zero rows if that suite ran first in the same session. Insert the row
     * (mirroring T023's verified production shape -- type=0 is
     * ModulesApplication::MODULE_TYPE_CUSTOM, the column core's gate actually
     * checks) when absent, else update mod_active on the existing row.
     */
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

    private function openDashboard(): void
    {
        $this->client->request(
            'GET',
            '/interface/patient_file/summary/demographics.php?set_pid=' . self::FIXTURE_PID
        );
        $this->client->wait(20)->until(
            fn($driver) => (bool) $driver->executeScript(
                'return document.getElementById("copilot-panel") !== null;'
            )
        );
    }

    /**
     * Fire a synchronous XHR POST to the relay from within the logged-in page
     * (carrying the session cookie), returning {status, body}.
     *
     * @param array<string, string> $fields
     * @return array{status: int, body: string}
     */
    private function postToRelay(array $fields): array
    {
        $script = <<<'JS'
            var url = arguments[0];
            var fields = arguments[1];
            var params = Object.keys(fields).map(function (k) {
                return encodeURIComponent(k) + '=' + encodeURIComponent(fields[k]);
            }).join('&');
            var xhr = new XMLHttpRequest();
            xhr.open('POST', url, false);
            xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
            try {
                xhr.send(params);
            } catch (e) {
                return JSON.stringify({ status: -1, body: String(e) });
            }
            return JSON.stringify({ status: xhr.status, body: xhr.responseText });
        JS;

        $json = (string) $this->client->executeScript($script, [self::RELAY_PATH, $fields]);
        /** @var array{status: int, body: string} $decoded */
        $decoded = json_decode($json, true);
        $this->assertIsArray($decoded);
        return $decoded;
    }

    private function readCsrfToken(): string
    {
        return (string) $this->client->executeScript(
            'return (typeof window.OE_COPILOT_CSRF === "string") ? window.OE_COPILOT_CSRF : "";'
        );
    }

    private function assertNoTokenLeak(string $body, string $context): void
    {
        foreach (['eyJ', 'Bearer ', 'access_token', 'client_secret', 'refresh_token'] as $needle) {
            $this->assertStringNotContainsString(
                $needle,
                $body,
                'Service-token material must never reach the browser (' . $context . '): ' . $needle
            );
        }
    }

    // ------------------------------------------------------------ C1 / C2 / C4

    #[Test]
    public function testValidRequestPassesCsrfAndAclReachesAgentAndNeverLeaksToken(): void
    {
        $this->base();
        try {
            $this->login(LoginTestData::username, LoginTestData::password);
            $this->openDashboard();

            // The panel must expose a per-session CSRF token to the client.
            $csrf = $this->readCsrfToken();
            $this->assertNotSame('', $csrf, 'Panel must expose window.OE_COPILOT_CSRF for the relay POST.');

            $start = microtime(true);
            $result = $this->postToRelay([
                'csrf_token_form' => $csrf,
                'message' => 'What medications is this patient on?',
                'patient_id' => $this->openPatientUuid,
            ]);
            $elapsed = microtime(true) - $start;

            // It passed CSRF and ACL (not a 403) and reached the token/agent path.
            // With no LLM key the agent errors, so the relay returns its graceful
            // agent-unavailable status; with a key it would be a 200 reply. Either
            // way it is NOT a CSRF/ACL rejection and NOT the login page.
            $this->assertNotSame(403, $result['status'], 'A valid CSRF+open-patient request must not be rejected.');
            $this->assertContains(
                $result['status'],
                [200, 502],
                'A valid request must reach the agent path (200 reply or 502 agent-unavailable). Body: ' . $result['body']
            );
            $this->assertLessThan(20.0, $elapsed, 'The relay must not hang on the agent (bounded wall-clock).');

            // C2: the service token the relay minted server-side must not appear
            // anywhere in the browser-facing response.
            $this->assertNoTokenLeak($result['body'], 'valid request');
        } catch (\Throwable $e) {
            $this->client->quit();
            throw $e;
        }
        $this->client->quit();
    }

    #[Test]
    public function testMissingAndGarbageCsrfAreRejected(): void
    {
        $this->base();
        try {
            $this->login(LoginTestData::username, LoginTestData::password);
            $this->openDashboard();

            $noCsrf = $this->postToRelay([
                'message' => 'hi',
                'patient_id' => $this->openPatientUuid,
            ]);
            $this->assertSame(403, $noCsrf['status'], 'A request with no CSRF token must be rejected. Body: ' . $noCsrf['body']);
            $this->assertNoTokenLeak($noCsrf['body'], 'missing csrf');

            $garbage = $this->postToRelay([
                'csrf_token_form' => 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
                'message' => 'hi',
                'patient_id' => $this->openPatientUuid,
            ]);
            $this->assertSame(403, $garbage['status'], 'A request with a garbage CSRF token must be rejected. Body: ' . $garbage['body']);
        } catch (\Throwable $e) {
            $this->client->quit();
            throw $e;
        }
        $this->client->quit();
    }

    #[Test]
    public function testPatientNotOpenInSessionIsRejected(): void
    {
        if ($this->otherPatientUuid === '') {
            $this->markTestSkipped('No second patient available to probe the ACL/open-patient gate.');
        }

        $this->base();
        try {
            $this->login(LoginTestData::username, LoginTestData::password);
            $this->openDashboard(); // opens pid=100 in the session

            $csrf = $this->readCsrfToken();
            // Valid CSRF, but a DIFFERENT patient than the one open in the session.
            $result = $this->postToRelay([
                'csrf_token_form' => $csrf,
                'message' => 'What medications is this patient on?',
                'patient_id' => $this->otherPatientUuid,
            ]);

            $this->assertSame(
                403,
                $result['status'],
                'A patient not open in the session must be denied. Body: ' . $result['body']
            );
        } catch (\Throwable $e) {
            $this->client->quit();
            throw $e;
        }
        $this->client->quit();
    }

    #[Test]
    public function testUnauthenticatedRequestIsRejected(): void
    {
        // A cookie-less request to the session-authenticated relay must NOT be
        // served the relay's JSON contract; core auth intercepts it. Property,
        // not proxy: assert the body is not our reply payload and carries no token.
        $baseUrl = getenv('SELENIUM_BASE_URL', true) ?: 'http://openemr';
        $context = stream_context_create([
            'ssl' => ['verify_peer' => false, 'verify_peer_name' => false],
            'http' => [
                'method' => 'POST',
                'header' => "Content-Type: application/x-www-form-urlencoded\r\n",
                'content' => http_build_query([
                    'message' => 'hi',
                    'patient_id' => $this->openPatientUuid,
                ]),
                'ignore_errors' => true,
                'timeout' => 20,
            ],
        ]);
        $body = @file_get_contents(rtrim((string) $baseUrl, '/') . self::RELAY_PATH, false, $context);
        $this->assertIsString($body, 'Expected a response body from the relay endpoint.');

        // Must not have produced a successful relay reply for an anonymous caller.
        $this->assertStringNotContainsString('"reply"', $body, 'An unauthenticated caller must not get a relay reply.');
        $this->assertStringNotContainsString('"conversation_id"', $body, 'An unauthenticated caller must not get a conversation id.');
        $this->assertNoTokenLeak($body, 'unauthenticated');
    }

    // -------------------------------------------------------------------- C3

    #[Test]
    public function testSuccessfulReplyRendersAsAgentBubbleAndFailureShowsGracefulState(): void
    {
        $this->base();
        try {
            $this->login(LoginTestData::username, LoginTestData::password);
            $this->openDashboard();

            // ---- success path: stub the relay response, drive sendMessage, and
            // assert an assistant bubble appears rendered via textContent.
            $htmlLookalike = '<img src=x onerror=alert(1)> take 10mg';
            $renderJson = (string) $this->client->executeScript(<<<JS
                var REPLY = arguments[0];
                window.fetch = function () {
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: function () { return Promise.resolve({ reply: REPLY, conversation_id: 'conv-e2e-1' }); },
                        text: function () { return Promise.resolve(JSON.stringify({ reply: REPLY, conversation_id: 'conv-e2e-1' })); }
                    });
                };
                var input = document.getElementById('copilot-input');
                var sendBtn = document.getElementById('copilot-send');
                input.value = 'question';
                sendBtn.click();
                return JSON.stringify({ ok: true });
            JS, [$htmlLookalike]);
            $this->assertStringContainsString('"ok":true', $renderJson);

            // The agent bubble arrives asynchronously.
            $this->client->wait(15)->until(
                fn($driver) => (bool) $driver->executeScript(
                    'return document.querySelectorAll("#copilot-message-list .oe-copilot__bubble--agent").length >= 1;'
                )
            );

            $agentJson = (string) $this->client->executeScript(<<<'JS'
                var list = document.getElementById('copilot-message-list');
                var agent = list.querySelector('.oe-copilot__bubble--agent');
                return JSON.stringify({
                    agentText: agent ? agent.textContent : null,
                    injectedImgCount: list.querySelectorAll('img').length,
                    userBubbleCount: list.querySelectorAll('.oe-copilot__bubble--user').length
                });
            JS);
            /** @var array{agentText: ?string, injectedImgCount: int, userBubbleCount: int} $agent */
            $agent = json_decode($agentJson, true);
            $this->assertSame($htmlLookalike, $agent['agentText'], 'Agent reply must render verbatim via textContent.');
            $this->assertSame(0, $agent['injectedImgCount'], 'Agent reply HTML must never be parsed as markup.');
            $this->assertSame(1, $agent['userBubbleCount'], 'The user echo bubble must remain (behavior preserved).');

            // ---- failure path: stub the relay to be unreachable; assert a
            // graceful state with no exception text leaks into the DOM.
            $failJson = (string) $this->client->executeScript(<<<'JS'
                window.fetch = function () { return Promise.reject(new TypeError('Failed to fetch at copilot-relay.php:1')); };
                var input = document.getElementById('copilot-input');
                var sendBtn = document.getElementById('copilot-send');
                input.value = 'another question';
                sendBtn.click();
                return JSON.stringify({ ok: true });
            JS);
            $this->assertStringContainsString('"ok":true', $failJson);

            $this->client->wait(15)->until(
                fn($driver) => (bool) $driver->executeScript(
                    'var el = document.getElementById("copilot-status");'
                    . 'return el !== null && el.textContent.trim().length > 0;'
                )
            );

            $statusText = (string) $this->client->executeScript(
                'var el = document.getElementById("copilot-status"); return el ? el.textContent : "";'
            );
            $this->assertNotSame('', $statusText, 'A graceful failure message must be shown.');
            foreach (['TypeError', 'Failed to fetch', 'copilot-relay.php', 'Error:', 'undefined'] as $leak) {
                $this->assertStringNotContainsString(
                    $leak,
                    $statusText,
                    'The graceful failure state must not leak internal error text: ' . $leak
                );
            }
        } catch (\Throwable $e) {
            $this->client->quit();
            throw $e;
        }
        $this->client->quit();
    }
}
