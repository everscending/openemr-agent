<?php

/**
 * Clinical Co-Pilot relay -> agent chat request (T021).
 *
 * The T011 /chat contract as a typed value object: the fields the relay forwards
 * server-side to the agent. `token` is the service bearer; it is carried here only
 * to hand to the agent transport and must never be echoed to the browser or logs.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

final readonly class CopilotChatRequest
{
    public function __construct(
        public string $message,
        public string $patientId,
        public string $token,
        public ?string $conversationId = null,
    ) {
    }
}
