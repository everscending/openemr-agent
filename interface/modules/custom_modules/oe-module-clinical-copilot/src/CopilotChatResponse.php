<?php

/**
 * Clinical Co-Pilot agent -> relay chat response (T021).
 *
 * The subset of the T011 ChatResponse the panel needs: the reply text, the
 * conversation id (so follow-up turns reuse it) and the fallback flag. The
 * service token is deliberately absent from this type — it never travels back
 * toward the browser.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

final readonly class CopilotChatResponse
{
    public function __construct(
        public string $conversationId,
        public string $reply,
        public bool $fallback = false,
    ) {
    }
}
