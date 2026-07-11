<?php

/**
 * Session-scoped patient authorization for the relay (T021 criterion 1).
 *
 * OpenEMR has no per-patient ACL; "may this session see this patient" is the
 * conjunction of (a) the patient being the one currently OPEN in the session
 * ($_SESSION['pid'], the load-bearing gate — capability alone does not restrict
 * WHICH patient), (b) the caller holding the patients/demo capability, and (c)
 * the squad restriction parity that demographics.php itself applies. The POSTed
 * identifier is never trusted: it is resolved to a local pid and every check is
 * made against the session, exactly as the authenticated UI operates.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use Symfony\Component\HttpFoundation\Session\SessionInterface;

final class SessionPatientAccessGuard implements PatientAccessGuard
{
    public function resolveAccessiblePid(string $patientUuid, SessionInterface $session): ?int
    {
        if (!UuidRegistry::isValidStringUUID($patientUuid)) {
            return null;
        }

        $bytes = UuidRegistry::uuidToBytes($patientUuid);
        $row = QueryUtils::querySingleRow(
            'SELECT `pid`, `squad` FROM `patient_data` WHERE `uuid` = ?',
            [$bytes]
        );
        if (!is_array($row) || !isset($row['pid'])) {
            return null;
        }

        $pid = $this->asPid($row['pid']);
        if ($pid === null) {
            return null;
        }

        // (a) It must be the patient currently OPEN in this session.
        $sessionPidRaw = $session->get('pid');
        $sessionPid = is_numeric($sessionPidRaw) ? (int) $sessionPidRaw : 0;
        if ($sessionPid === 0 || $pid !== $sessionPid) {
            return null;
        }

        // (b) Capability check.
        if (!AclMain::aclCheckCore('patients', 'demo')) {
            return null;
        }

        // (c) Squad restriction parity with demographics.php.
        $squad = $row['squad'] ?? null;
        if (is_string($squad) && $squad !== '' && !AclMain::aclCheckCore('squads', $squad)) {
            return null;
        }

        return $pid;
    }

    private function asPid(mixed $value): ?int
    {
        if (is_int($value)) {
            return $value;
        }
        if (is_string($value) && ctype_digit($value)) {
            return (int) $value;
        }
        return null;
    }
}
