<?php

/**
 * Server-side transport to the co-pilot agent's /chat endpoint (T021).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

interface CopilotAgentChatClient
{
    /**
     * Forward a chat turn to the agent and return its answer.
     *
     * @throws AgentUnavailableException when the agent is unreachable, times out,
     *   returns a non-2xx status, or returns an unparseable body.
     */
    public function chat(CopilotChatRequest $request): CopilotChatResponse;
}
