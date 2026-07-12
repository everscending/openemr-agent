<?php

/**
 * PHP -> agent correlation ID propagation (T046).
 *
 * PRD.md:308-310 / ARCHITECTURE.md:65-67,99 require a correlation ID joinable
 * from logs alone across the whole request lifecycle. Before this ticket the
 * ID existed only inside the agent process; PHP never minted or sent one.
 *
 *   C1 - HttpCopilotAgentChatClient mints a fresh UUIDv4 per call and sends it
 *        as the literal `X-Correlation-ID` request header, proven against a
 *        REAL local HTTP server (not a mock of the transport) that records
 *        the header it actually received on the wire. A caller-supplied ID is
 *        forwarded byte-identical, never re-minted.
 *   C2 - CopilotRelayController's failure log includes `correlation_id`, and
 *        it is the EXACT SAME value handed to the agent client for that same
 *        request (checked at the CopilotChatRequest seam, not re-derived) —
 *        otherwise the log entry and the agent-side record could never be
 *        joined. Two independent failed requests mint two distinct IDs.
 *
 * (Criterion 3 - outbound FHIR calls forwarding the active correlation ID -
 * and the agent-side "prefer incoming header" half of criterion 1 are agent
 * (Python) behavior, covered in agent/tests/test_fhir_client.py and
 * agent/tests/test_chat.py / test_audit_bridge.py respectively.)
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Modules\ClinicalCopilot;

use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Modules\ClinicalCopilot\AgentUnavailableException;
use OpenEMR\Modules\ClinicalCopilot\CopilotChatRequest;
use OpenEMR\Modules\ClinicalCopilot\CopilotChatResponse;
use OpenEMR\Modules\ClinicalCopilot\CopilotRelayController;
use OpenEMR\Modules\ClinicalCopilot\HttpCopilotAgentChatClient;
use PHPUnit\Framework\TestCase;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Session\Session;
use Symfony\Component\HttpFoundation\Session\SessionInterface;
use Symfony\Component\HttpFoundation\Session\Storage\MockArraySessionStorage;

final class CorrelationIdBoundaryTest extends TestCase
{
    private const PATIENT_UUID = 'a23a078e-0da5-4b07-ab9e-ad99fbde1b89';
    private const TOKEN = 'service-token-corr-test';

    private const UUID_V4_PATTERN = '/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i';

    /** @var resource|false|null */
    private static $serverProcess = null;

    private static string $baseUrl = '';

    private static string $logFile = '';

    public static function setUpBeforeClass(): void
    {
        // The module namespace is not in the root composer autoloader; register
        // a PSR-4 mapping for it so the production classes under test load.
        $srcDir = dirname(__DIR__, 5)
            . '/interface/modules/custom_modules/oe-module-clinical-copilot/src';
        spl_autoload_register(static function (string $class) use ($srcDir): void {
            $prefix = 'OpenEMR\\Modules\\ClinicalCopilot\\';
            if (!str_starts_with($class, $prefix)) {
                return;
            }
            $relative = substr($class, strlen($prefix));
            $file = $srcDir . '/' . str_replace('\\', '/', $relative) . '.php';
            if (is_file($file)) {
                require_once $file;
            }
        });

        $logFile = tempnam(sys_get_temp_dir(), 'copilot_corr_');
        if ($logFile === false) {
            throw new \RuntimeException('failed to allocate a temp file for the correlation-id capture log');
        }
        self::$logFile = $logFile;

        $port = self::findFreePort();
        self::$baseUrl = 'http://127.0.0.1:' . $port;

        putenv('CORRELATION_LOG_FILE=' . self::$logFile);

        $router = __DIR__ . '/fixtures/correlation_id_capture_router.php';
        $descriptors = [
            1 => ['file', '/dev/null', 'w'],
            2 => ['file', '/dev/null', 'w'],
        ];
        $pipes = [];
        $process = proc_open(
            [PHP_BINARY, '-S', '127.0.0.1:' . $port, $router],
            $descriptors,
            $pipes,
        );
        if ($process === false) {
            throw new \RuntimeException('failed to start the php -S correlation-id capture server');
        }
        self::$serverProcess = $process;

        self::waitForServerReady('127.0.0.1', $port, 5.0);
    }

    public static function tearDownAfterClass(): void
    {
        if (is_resource(self::$serverProcess)) {
            proc_terminate(self::$serverProcess);
            proc_close(self::$serverProcess);
        }
        self::$serverProcess = null;

        if (self::$logFile !== '' && is_file(self::$logFile)) {
            unlink(self::$logFile);
        }
        putenv('CORRELATION_LOG_FILE');
    }

    protected function setUp(): void
    {
        // Fresh per test: each test reads only the lines its own calls produced.
        file_put_contents(self::$logFile, '');
    }

    private static function findFreePort(): int
    {
        $sock = stream_socket_server('tcp://127.0.0.1:0', $errno, $errstr);
        if ($sock === false) {
            throw new \RuntimeException('failed to allocate a free local port: ' . $errstr);
        }
        $name = stream_socket_get_name($sock, false);
        fclose($sock);
        $parts = explode(':', (string) $name);
        return (int) end($parts);
    }

    private static function waitForServerReady(string $host, int $port, float $timeoutSeconds): void
    {
        $deadline = microtime(true) + $timeoutSeconds;
        while (microtime(true) < $deadline) {
            $conn = @fsockopen($host, $port, $errno, $errstr, 0.2);
            if ($conn !== false) {
                fclose($conn);
                return;
            }
            usleep(50000);
        }
        throw new \RuntimeException('the correlation-id capture test server did not become ready in time');
    }

    /**
     * @return list<string>
     */
    private function readLogLines(): string|array
    {
        $contents = file_get_contents(self::$logFile);
        $this->assertIsString($contents);
        $lines = array_values(array_filter(explode("\n", $contents), static fn (string $l): bool => $l !== ''));
        return $lines;
    }

    // ---------------------------------------------------------- C1: transport

    public function testHttpClientMintsFreshUuidV4PerCallWhenNoneSupplied(): void
    {
        $client = new HttpCopilotAgentChatClient(self::$baseUrl, 5);
        $client->chat(new CopilotChatRequest('hi', self::PATIENT_UUID, self::TOKEN, null));
        $client->chat(new CopilotChatRequest('hi again', self::PATIENT_UUID, self::TOKEN, null));

        $lines = $this->readLogLines();
        $this->assertCount(2, $lines, 'Expected exactly two outbound requests to reach the test double.');
        foreach ($lines as $line) {
            $this->assertMatchesRegularExpression(
                self::UUID_V4_PATTERN,
                $line,
                'The X-Correlation-ID header received on the wire must be a valid UUIDv4.'
            );
        }
        $this->assertNotSame(
            $lines[0],
            $lines[1],
            'Each call must mint a fresh correlation ID, not reuse a fixed/cached value.'
        );
    }

    public function testHttpClientForwardsAnExplicitlySuppliedCorrelationIdVerbatim(): void
    {
        // A distinctive, PHP-side-minted sentinel: proves the client forwards
        // what it is given rather than always minting its own.
        $distinctive = 'f47ac10b-58cc-4372-a567-0e02b2c3d479';

        $client = new HttpCopilotAgentChatClient(self::$baseUrl, 5);
        $client->chat(new CopilotChatRequest('hi', self::PATIENT_UUID, self::TOKEN, null, $distinctive));

        $lines = $this->readLogLines();
        $this->assertCount(1, $lines);
        $this->assertSame(
            $distinctive,
            $lines[0],
            'A caller-supplied correlation ID must reach the wire byte-identical, never re-minted.'
        );
    }

    // ------------------------------------------------------- C2: relay logs

    private function makeSession(): SessionInterface
    {
        $session = new Session(new MockArraySessionStorage());
        $session->set('csrf_private_key', random_bytes(32));
        return $session;
    }

    private function makePost(SessionInterface $session): Request
    {
        $params = [
            'csrf_token_form' => CsrfUtils::collectCsrfToken($session),
            'message' => 'What medications is this patient on?',
            'patient_id' => self::PATIENT_UUID,
        ];
        $request = Request::create('/copilot-relay.php', 'POST', $params);
        $request->setSession($session);
        return $request;
    }

    public function testRelayControllerForwardsAFreshUuidV4CorrelationIdToTheAgentOnHappyPath(): void
    {
        $session = $this->makeSession();
        $agent = new FakeAgentChatClient(new CopilotChatResponse('conv-1', 'ok reply'));
        $controller = new CopilotRelayController(
            $agent,
            new FakeServiceTokenProvider(self::TOKEN),
            new FakeAccessGuard(100),
            new RecordingLogger(),
        );

        $controller->handle($this->makePost($session), $session);

        $this->assertCount(1, $agent->calls);
        $sent = $agent->calls[0]->correlationId;
        $this->assertNotNull($sent, 'The relay must mint a correlation ID for every outbound agent call.');
        $this->assertMatchesRegularExpression(self::UUID_V4_PATTERN, $sent);
    }

    public function testRelayControllerFailureLogIncludesTheSameCorrelationIdSentToTheAgent(): void
    {
        $session = $this->makeSession();
        $agent = new FakeAgentChatClient(null, new AgentUnavailableException('boom'));
        $logger = new RecordingLogger();
        $controller = new CopilotRelayController(
            $agent,
            new FakeServiceTokenProvider(self::TOKEN),
            new FakeAccessGuard(100),
            $logger,
        );

        $controller->handle($this->makePost($session), $session);

        $this->assertCount(1, $agent->calls);
        $sentCorrelationId = $agent->calls[0]->correlationId;
        $this->assertNotNull($sentCorrelationId);

        $errorRecords = array_values(array_filter(
            $logger->records,
            static fn (array $r): bool => $r[1] === 'clinical_copilot_relay_agent_unavailable'
        ));
        $this->assertCount(1, $errorRecords, 'Expected exactly one agent_unavailable error log entry.');
        $context = $errorRecords[0][2];
        $this->assertArrayHasKey('correlation_id', $context);
        $this->assertSame(
            $sentCorrelationId,
            $context['correlation_id'],
            'The logged correlation ID must be the EXACT ID sent to the agent for this call, '
                . 'otherwise the failure log and the agent-side record can never be joined.'
        );
    }

    public function testTwoSeparateFailedRequestsMintTwoDistinctCorrelationIds(): void
    {
        $session = $this->makeSession();
        $logger = new RecordingLogger();

        $agent1 = new FakeAgentChatClient(null, new AgentUnavailableException('boom-1'));
        (new CopilotRelayController(
            $agent1,
            new FakeServiceTokenProvider(self::TOKEN),
            new FakeAccessGuard(100),
            $logger,
        ))->handle($this->makePost($session), $session);

        $agent2 = new FakeAgentChatClient(null, new AgentUnavailableException('boom-2'));
        (new CopilotRelayController(
            $agent2,
            new FakeServiceTokenProvider(self::TOKEN),
            new FakeAccessGuard(100),
            $logger,
        ))->handle($this->makePost($session), $session);

        $errorRecords = array_values(array_filter(
            $logger->records,
            static fn (array $r): bool => $r[1] === 'clinical_copilot_relay_agent_unavailable'
        ));
        $this->assertCount(2, $errorRecords);
        $this->assertNotSame(
            $errorRecords[0][2]['correlation_id'],
            $errorRecords[1][2]['correlation_id'],
            'Each inbound relay request must mint its own correlation ID, never a fixed process-wide value.'
        );
    }
}
