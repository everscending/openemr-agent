<?php

/**
 * Decides whether the currently-authenticated session may act on a patient
 * identifier the browser supplied (T021 criterion 1). The relay never trusts the
 * POSTed identifier; this guard independently resolves and authorizes it.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use Symfony\Component\HttpFoundation\Session\SessionInterface;

interface PatientAccessGuard
{
    /**
     * @return int|null the local pid if the session may access this patient,
     *   or null on any denial (unknown patient, not the session's open patient,
     *   or insufficient ACL).
     */
    public function resolveAccessiblePid(string $patientUuid, SessionInterface $session): ?int;
}
