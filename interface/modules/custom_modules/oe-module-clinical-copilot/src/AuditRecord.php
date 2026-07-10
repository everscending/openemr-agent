<?php

/**
 * Parsed, fully-validated audit invocation record (T016).
 *
 * A value object produced by AuditRecordParser only after the entire wire
 * record has been validated. Downstream code works with guaranteed-valid
 * fields — "parse, don't validate".
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    OpenEMR Clinical Co-Pilot <support@open-emr.org>
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use DateTimeImmutable;

final readonly class AuditRecord
{
    public function __construct(
        public string $userTokenHash,
        public string $patientId,
        public string $correlationId,
        public string $conversationId,
        public DateTimeImmutable $occurredAt,
        public int $claimsTotal,
        public int $claimsPassed,
        public int $claimsStripped,
        public string $outcome,
        public ?string $degraded,
        public ?string $fallbackReason,
    ) {
    }
}
