<?php

/**
 * Raised when the co-pilot agent cannot be reached or did not return a usable
 * answer (T021 criterion 4). The relay maps it to a single graceful failure
 * state; its message is never surfaced to the browser or written to a log — the
 * class name only, exactly like the audit bridge.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use RuntimeException;

final class AgentUnavailableException extends RuntimeException
{
}
