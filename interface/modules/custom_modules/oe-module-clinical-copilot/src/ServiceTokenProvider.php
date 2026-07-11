<?php

/**
 * Supplies the service-account bearer the relay forwards to the agent (T021).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

interface ServiceTokenProvider
{
    /**
     * @throws AgentUnavailableException when a token cannot be obtained (the panel
     *   treats "no token to reach the assistant" as "couldn't reach the assistant").
     */
    public function getToken(): string;
}
