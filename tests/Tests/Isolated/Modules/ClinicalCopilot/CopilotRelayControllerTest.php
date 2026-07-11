<?php

/**
 * Clinical Co-Pilot relay controller unit test (T021).
 *
 * Exercises the REAL relay controller against the REAL OpenEMR CsrfUtils and a
 * REAL Symfony session, with the agent HTTP client, service-token provider and
 * patient-access guard supplied as fakes so the load-bearing safety properties
 * can be asserted deterministically and without a database:
 *
 *   C1 - CSRF-protected; a missing / garbage / wrong-session CSRF token is
 *        rejected, and an access-denied patient is rejected, and every such
 *        rejection makes ZERO outbound agent calls (counted at the fake seam).
 *   C2 - the service token is forwarded to the agent server-side but is NEVER
 *        present in the response the browser receives (full status+headers+body
 *        swept) nor in any log line (logger sweep).
 *   C3 - a happy-path answer is returned as reply text + conversation_id.
 *   C4 - an unreachable agent yields a graceful failure that leaks no exception
 *        message, class name, file path or stack fragment; and the REAL HTTP
 *        client is bounded (does not hang) against a dead / black-hole endpoint.
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
use OpenEMR\Modules\ClinicalCopilot\CopilotAgentChatClient;
use OpenEMR\Modules\ClinicalCopilot\CopilotChatRequest;
use OpenEMR\Modules\ClinicalCopilot\CopilotChatResponse;
use OpenEMR\Modules\ClinicalCopilot\CopilotRelayController;
use OpenEMR\Modules\ClinicalCopilot\HttpCopilotAgentChatClient;
use OpenEMR\Modules\ClinicalCopilot\PatientAccessGuard;
use OpenEMR\Modules\ClinicalCopilot\ServiceTokenProvider;
use PHPUnit\Framework\TestCase;
use Psr\Log\AbstractLogger;
use Stringable;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\HttpFoundation\Session\Session;
use Symfony\Component\HttpFoundation\Session\SessionInterface;
use Symfony\Component\HttpFoundation\Session\Storage\MockArraySessionStorage;

final class CopilotRelayControllerTest extends TestCase
{
    /**
     * The service bearer the relay obtains server-side. A recognisable
     * sentinel (JWT-shaped) so any leak into the browser response or the logs
     * is unmistakable.
     */
    private const SENTINEL_TOKEN = 'eyJSENTINEL.SERVICE.TOKEN-do-not-leak-9f8a7b6c5d4e3f2a1b';

    private const PATIENT_UUID = 'a23a078e-0da5-4b07-ab9e-ad99fbde1b89';
    private const RESOLVED_PID = 100;

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
    }

    private function makeSession(): SessionInterface
    {
        $session = new Session(new MockArraySessionStorage());
        $session->set('csrf_private_key', random_bytes(32));
        return $session;
    }

    /**
     * @param array<string, string|null> $overrides
     */
    private function makePost(SessionInterface $session, array $overrides = []): Request
    {
        $params = array_merge([
            'csrf_token_form' => CsrfUtils::collectCsrfToken($session),
            'message' => 'What medications is this patient on?',
            'patient_id' => self::PATIENT_UUID,
            'conversation_id' => null,
        ], $overrides);

        // array_merge keeps an explicit null override; strip null-valued keys so
        // they are genuinely absent from the request bag (not present-but-null).
        $params = array_filter($params, static fn ($v): bool => $v !== null);

        $request = Request::create('/copilot-relay.php', 'POST', $params);
        $request->setSession($session);
        return $request;
    }

    private function controller(
        FakeAgentChatClient $agent,
        ?FakeAccessGuard $guard = null,
        ?FakeServiceTokenProvider $token = null,
        ?RecordingLogger $logger = null,
    ): CopilotRelayController {
        return new CopilotRelayController(
            $agent,
            $token ?? new FakeServiceTokenProvider(self::SENTINEL_TOKEN),
            $guard ?? new FakeAccessGuard(self::RESOLVED_PID),
            $logger ?? new RecordingLogger(),
        );
    }

    // -------------------------------------------------------------- C1 / C3

    public function testHappyPathReturnsReplyAndConversationIdAndCallsAgentOnce(): void
    {
        $session = $this->makeSession();
        $agent = new FakeAgentChatClient(new CopilotChatResponse('conv-xyz', 'She takes lisinopril 10mg daily.'));
        $controller = $this->controller($agent);

        $response = $controller->handle($this->makePost($session), $session);

        $this->assertSame(200, $response->getStatusCode());
        /** @var array<string, mixed> $body */
        $body = json_decode((string) $response->getContent(), true);
        $this->assertSame('She takes lisinopril 10mg daily.', $body['reply']);
        $this->assertSame('conv-xyz', $body['conversation_id']);

        $this->assertCount(1, $agent->calls, 'Exactly one outbound agent call on the happy path.');
        // The agent must be called with the T011 contract, bound to the guard's
        // resolved patient uuid and the message, carrying the service token.
        $sent = $agent->calls[0];
        $this->assertSame('What medications is this patient on?', $sent->message);
        $this->assertSame(self::PATIENT_UUID, $sent->patientId);
        $this->assertSame(self::SENTINEL_TOKEN, $sent->token, 'Service token must be forwarded server-side.');
    }

    public function testNonPostIsRejectedWithoutCallingAgent(): void
    {
        $session = $this->makeSession();
        $agent = new FakeAgentChatClient(new CopilotChatResponse('c', 'r'));
        $controller = $this->controller($agent);

        $request = Request::create('/copilot-relay.php', 'GET');
        $request->setSession($session);
        $response = $controller->handle($request, $session);

        $this->assertSame(405, $response->getStatusCode());
        $this->assertCount(0, $agent->calls);
    }

    public function testMissingCsrfTokenIsRejectedWithZeroAgentCalls(): void
    {
        $session = $this->makeSession();
        $agent = new FakeAgentChatClient(new CopilotChatResponse('c', 'r'));
        $controller = $this->controller($agent);

        // No csrf token at all in the body.
        $request = Request::create('/copilot-relay.php', 'POST', [
            'message' => 'hi',
            'patient_id' => self::PATIENT_UUID,
        ]);
        $request->setSession($session);
        $response = $controller->handle($request, $session);

        $this->assertSame(403, $response->getStatusCode());
        $this->assertCount(0, $agent->calls, 'A missing CSRF token must make zero agent calls.');
    }

    public function testGarbageCsrfTokenIsRejectedWithZeroAgentCalls(): void
    {
        $session = $this->makeSession();
        $agent = new FakeAgentChatClient(new CopilotChatResponse('c', 'r'));
        $controller = $this->controller($agent);

        $response = $controller->handle(
            $this->makePost($session, ['csrf_token_form' => 'not-a-valid-token']),
            $session
        );

        $this->assertSame(403, $response->getStatusCode());
        $this->assertCount(0, $agent->calls, 'A garbage CSRF token must make zero agent calls.');
    }

    public function testCsrfTokenMintedForADifferentSessionIsRejected(): void
    {
        // Adversarial: a syntactically valid token, but minted against a
        // different session's private key. Must not verify.
        $otherSession = $this->makeSession();
        $foreignToken = CsrfUtils::collectCsrfToken($otherSession);

        $session = $this->makeSession();
        $agent = new FakeAgentChatClient(new CopilotChatResponse('c', 'r'));
        $controller = $this->controller($agent);

        $response = $controller->handle(
            $this->makePost($session, ['csrf_token_form' => $foreignToken]),
            $session
        );

        $this->assertSame(403, $response->getStatusCode());
        $this->assertCount(0, $agent->calls);
    }

    public function testPatientAccessDeniedIsRejectedWithZeroAgentCalls(): void
    {
        $session = $this->makeSession();
        $agent = new FakeAgentChatClient(new CopilotChatResponse('c', 'r'));
        // Guard returns null: the current session may not see this patient.
        $guard = new FakeAccessGuard(null);
        $controller = $this->controller($agent, $guard);

        $response = $controller->handle($this->makePost($session), $session);

        $this->assertSame(403, $response->getStatusCode());
        $this->assertCount(0, $agent->calls, 'A patient the session cannot access must make zero agent calls.');
        // The guard must have been consulted with the exact POSTed uuid (the
        // relay must not trust the identifier blindly).
        $this->assertSame(
            [self::PATIENT_UUID],
            $guard->queried,
            'The relay must ask the access guard about the POSTed patient uuid.'
        );
    }

    public function testEmptyMessageIsRejectedWithZeroAgentCalls(): void
    {
        $session = $this->makeSession();
        $agent = new FakeAgentChatClient(new CopilotChatResponse('c', 'r'));
        $controller = $this->controller($agent);

        $response = $controller->handle(
            $this->makePost($session, ['message' => '   ']),
            $session
        );

        $this->assertSame(422, $response->getStatusCode());
        $this->assertCount(0, $agent->calls);
    }

    // -------------------------------------------------------------------- C2

    public function testServiceTokenNeverAppearsInTheBrowserResponse(): void
    {
        $session = $this->makeSession();
        $agent = new FakeAgentChatClient(new CopilotChatResponse('conv-1', 'ok reply'));
        $controller = $this->controller($agent);

        $response = $controller->handle($this->makePost($session), $session);
        $this->assertSame(200, $response->getStatusCode());

        // Property, not proxy: sweep the ENTIRE serialized HTTP message the
        // browser receives (status line + all headers + body), not a named field.
        $fullMessage = (string) $response;
        $this->assertStringNotContainsString(
            self::SENTINEL_TOKEN,
            $fullMessage,
            'The service token must never appear anywhere in the browser-facing response.'
        );
        // And it WAS actually forwarded to the agent (so absence above is real
        // suppression, not "the token was never used").
        $this->assertSame(self::SENTINEL_TOKEN, $agent->calls[0]->token);
    }

    public function testServiceTokenNeverAppearsInAnyLogLine(): void
    {
        $session = $this->makeSession();
        $logger = new RecordingLogger();
        // Force the failure path too, since error logging is most likely there.
        $agent = new FakeAgentChatClient(null, new AgentUnavailableException(
            'connect to http://copilot:8080 failed using bearer ' . self::SENTINEL_TOKEN
        ));
        $controller = $this->controller($agent, null, null, $logger);

        $controller->handle($this->makePost($session), $session);

        // Also drive a happy path through the same logger.
        $agent2 = new FakeAgentChatClient(new CopilotChatResponse('c', 'r'));
        $this->controller($agent2, null, null, $logger)->handle($this->makePost($session), $session);

        $dump = json_encode($logger->records);
        $this->assertNotFalse($dump);
        $this->assertStringNotContainsString(
            self::SENTINEL_TOKEN,
            $dump,
            'The service token must never be written to any log line.'
        );
    }

    // -------------------------------------------------------------------- C4

    public function testUnreachableAgentYieldsGracefulLeakFreeFailure(): void
    {
        $session = $this->makeSession();
        // The exception message deliberately carries internal detail that must
        // NOT reach the browser.
        $agent = new FakeAgentChatClient(null, new AgentUnavailableException(
            'cURL error 7: Failed to connect to copilot port 8080 at /app/src/HttpCopilotAgentChatClient.php:123'
        ));
        $controller = $this->controller($agent);

        $response = $controller->handle($this->makePost($session), $session);

        // A graceful, non-hanging failure state (not a 200 success, not a 500 raw).
        $this->assertGreaterThanOrEqual(500, $response->getStatusCode());
        $body = (string) $response->getContent();

        foreach (
            [
                'cURL',
                'AgentUnavailableException',
                'HttpCopilotAgentChatClient',
                '.php',
                'Failed to connect',
                'port 8080',
                'Stack trace',
                '#0',
            ] as $leak
        ) {
            $this->assertStringNotContainsString(
                $leak,
                $body,
                'Graceful failure body must not leak: ' . $leak
            );
        }
    }

    public function testRealHttpClientIsBoundedAgainstADeadEndpoint(): void
    {
        // Connection-refused: a closed local port. Must throw the graceful
        // exception type quickly, never hang.
        $client = new HttpCopilotAgentChatClient('http://127.0.0.1:9', 3);
        $request = new CopilotChatRequest('hi', self::PATIENT_UUID, self::SENTINEL_TOKEN, null);

        $start = microtime(true);
        $threw = false;
        try {
            $client->chat($request);
        } catch (AgentUnavailableException) {
            $threw = true;
        }
        $elapsed = microtime(true) - $start;

        $this->assertTrue($threw, 'A refused connection must surface as AgentUnavailableException.');
        $this->assertLessThan(10.0, $elapsed, 'A refused connection must fail fast, never hang.');
    }

    public function testRealHttpClientTimesOutBoundedAgainstABlackHole(): void
    {
        // 10.255.255.1 is a non-routable/black-hole address: connect() hangs
        // until the bounded connect timeout fires. Proves the client abandons a
        // hung agent within a bounded wall-clock, rather than waiting forever.
        $timeout = 3;
        $client = new HttpCopilotAgentChatClient('http://10.255.255.1:8080', $timeout);
        $request = new CopilotChatRequest('hi', self::PATIENT_UUID, self::SENTINEL_TOKEN, null);

        $start = microtime(true);
        $threw = false;
        try {
            $client->chat($request);
        } catch (AgentUnavailableException) {
            $threw = true;
        }
        $elapsed = microtime(true) - $start;

        $this->assertTrue($threw, 'A hung agent must surface as AgentUnavailableException.');
        $this->assertLessThan(
            $timeout + 6.0,
            $elapsed,
            'The client must abandon a hung agent within a bounded wall-clock.'
        );
    }
}

/**
 * Fake agent client: records the requests it receives (the transport seam for
 * counting outbound calls) and returns a canned response or throws.
 */
final class FakeAgentChatClient implements CopilotAgentChatClient
{
    /** @var list<CopilotChatRequest> */
    public array $calls = [];

    public function __construct(
        private readonly ?CopilotChatResponse $response,
        private readonly ?AgentUnavailableException $throw = null,
    ) {
    }

    public function chat(CopilotChatRequest $request): CopilotChatResponse
    {
        $this->calls[] = $request;
        if ($this->throw !== null) {
            throw $this->throw;
        }
        if ($this->response === null) {
            throw new AgentUnavailableException('no response configured');
        }
        return $this->response;
    }
}

final class FakeServiceTokenProvider implements ServiceTokenProvider
{
    public function __construct(private readonly string $token)
    {
    }

    public function getToken(string $patientUuid = ''): string
    {
        return $this->token;
    }
}

final class FakeAccessGuard implements PatientAccessGuard
{
    /** @var list<string> */
    public array $queried = [];

    public function __construct(private readonly ?int $pid)
    {
    }

    public function resolveAccessiblePid(string $patientUuid, SessionInterface $session): ?int
    {
        $this->queried[] = $patientUuid;
        return $this->pid;
    }
}

final class RecordingLogger extends AbstractLogger
{
    /** @var list<array{mixed, string, array<string, mixed>}> */
    public array $records = [];

    /**
     * @param array<mixed> $context
     */
    public function log($level, string|Stringable $message, array $context = []): void
    {
        $this->records[] = [$level, (string) $message, $context];
    }
}
