<?php

/**
 * cURL-based transport to the co-pilot agent's /chat endpoint (T021).
 *
 * Bounded on both connect and total time so a down or hung agent is abandoned
 * within a fixed wall-clock (criterion 4) rather than hanging the relay request.
 * Every transport error, non-2xx status, or unparseable body surfaces as a single
 * AgentUnavailableException whose message carries no browser-bound detail.
 *
 * T046: every call carries an `X-Correlation-ID` header so the agent joins its
 * logs/spans/audit record to this same request. Uses `$request->correlationId`
 * when the caller supplied one (the normal path — CopilotRelayController mints
 * it); mints its own UUIDv4 fallback only when constructed/called directly
 * without one, so the header is never absent.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use Ramsey\Uuid\Uuid;

final class HttpCopilotAgentChatClient implements CopilotAgentChatClient
{
    private const CORRELATION_ID_HEADER = 'X-Correlation-ID';

    public function __construct(
        private readonly string $baseUrl,
        private readonly int $timeoutSeconds = 8,
    ) {
    }

    public function chat(CopilotChatRequest $request): CopilotChatResponse
    {
        $payload = [
            'message' => $request->message,
            'patient_id' => $request->patientId,
            'token' => $request->token,
        ];
        if ($request->conversationId !== null) {
            $payload['conversation_id'] = $request->conversationId;
        }

        $body = json_encode($payload);
        if ($body === false) {
            throw new AgentUnavailableException('failed to encode agent request');
        }

        $correlationId = $request->correlationId ?? Uuid::uuid4()->toString();

        $handle = curl_init(rtrim($this->baseUrl, '/') . '/chat');
        if ($handle === false) {
            throw new AgentUnavailableException('failed to initialize agent transport');
        }

        curl_setopt_array($handle, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'Accept: application/json',
                self::CORRELATION_ID_HEADER . ': ' . $correlationId,
            ],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => $this->timeoutSeconds,
            CURLOPT_TIMEOUT => $this->timeoutSeconds,
        ]);

        $raw = curl_exec($handle);
        $errno = curl_errno($handle);
        $status = (int) curl_getinfo($handle, CURLINFO_HTTP_CODE);

        if ($errno !== 0 || !is_string($raw)) {
            // Includes connect refused (7) and timeout (28): abandon, fail closed.
            throw new AgentUnavailableException('agent transport error (' . $errno . ')');
        }
        if ($status < 200 || $status >= 300) {
            throw new AgentUnavailableException('agent returned status ' . $status);
        }

        $decoded = json_decode($raw, true);
        if (
            !is_array($decoded)
            || !isset($decoded['reply']) || !is_string($decoded['reply'])
            || !isset($decoded['conversation_id']) || !is_string($decoded['conversation_id'])
        ) {
            throw new AgentUnavailableException('agent returned an unparseable response');
        }

        $fallback = isset($decoded['fallback']) && $decoded['fallback'] === true;

        return new CopilotChatResponse($decoded['conversation_id'], $decoded['reply'], $fallback);
    }
}
