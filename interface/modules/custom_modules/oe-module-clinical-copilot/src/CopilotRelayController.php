<?php

/**
 * Clinical Co-Pilot panel -> agent relay controller (T021).
 *
 * A same-origin, session-authenticated proxy: the panel POSTs a question here
 * (never to the agent directly — that would need a CORS hole in OpenEMR,
 * forbidden), and this controller, running inside the logged-in user's session,
 * CSRF-verifies it, independently authorizes the patient, obtains a
 * service-account bearer server-side, forwards the turn to the agent, and returns
 * only the reply text + conversation id to the browser.
 *
 * Failure directions (deliberate):
 *   - CSRF, patient authorization and input validation fail CLOSED, and every
 *     such rejection happens BEFORE any outbound agent call is made.
 *   - The service token is forwarded to the agent but never appears in the
 *     browser-facing response.
 *   - An unreachable/timed-out agent (or an un-mintable token) becomes ONE
 *     graceful failure state; the exception message/class never reaches the
 *     browser or a log — the class name only.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use OpenEMR\Common\Csrf\CsrfUtils;
use Psr\Log\LoggerInterface;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\HttpFoundation\Session\SessionInterface;

final class CopilotRelayController
{
    public function __construct(
        private readonly CopilotAgentChatClient $agent,
        private readonly ServiceTokenProvider $tokenProvider,
        private readonly PatientAccessGuard $accessGuard,
        private readonly LoggerInterface $logger,
    ) {
    }

    public function handle(Request $request, SessionInterface $session): Response
    {
        if ($request->getMethod() !== 'POST') {
            return $this->json(['error' => 'method_not_allowed'], Response::HTTP_METHOD_NOT_ALLOWED);
        }

        // CSRF gate — before anything else, and before any agent call.
        $csrf = $request->request->get('csrf_token_form');
        if (!is_string($csrf) || !CsrfUtils::verifyCsrfToken($csrf, $session)) {
            return $this->forbidden();
        }

        $message = $request->request->get('message');
        $patientId = $request->request->get('patient_id');
        $conversationId = $request->request->get('conversation_id');
        if (!is_string($message) || trim($message) === '' || !is_string($patientId) || $patientId === '') {
            return $this->json(['error' => 'unprocessable'], Response::HTTP_UNPROCESSABLE_ENTITY);
        }
        if (!is_string($conversationId) || $conversationId === '') {
            $conversationId = null;
        }

        // Independent patient authorization — never trust the POSTed identifier.
        $pid = $this->accessGuard->resolveAccessiblePid($patientId, $session);
        if ($pid === null) {
            return $this->forbidden();
        }

        try {
            $token = $this->tokenProvider->getToken();
            $answer = $this->agent->chat(
                new CopilotChatRequest($message, $patientId, $token, $conversationId)
            );
        } catch (AgentUnavailableException $e) {
            // The guard must never become the leak: class name only, never the
            // message (it may carry URLs, statuses, or the token).
            $this->logger->error('clinical_copilot_relay_agent_unavailable', [
                'exception_class' => $e::class,
            ]);
            return $this->json(['error' => 'agent_unavailable'], Response::HTTP_BAD_GATEWAY);
        }

        return $this->json([
            'reply' => $answer->reply,
            'conversation_id' => $answer->conversationId,
            'fallback' => $answer->fallback,
        ], Response::HTTP_OK);
    }

    private function forbidden(): JsonResponse
    {
        return $this->json(['error' => 'forbidden'], Response::HTTP_FORBIDDEN);
    }

    /**
     * @param array<string, scalar> $data
     */
    private function json(array $data, int $status): JsonResponse
    {
        return new JsonResponse($data, $status);
    }
}
