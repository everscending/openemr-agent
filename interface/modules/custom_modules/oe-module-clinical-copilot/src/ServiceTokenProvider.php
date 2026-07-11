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
     * Obtain the bearer the relay forwards to the agent, bound to the open patient.
     *
     * The patient uuid is threaded in from the relay call site (T027): a per-user /
     * per-patient mint binds it as the SMART launch context so FHIR reads are
     * patient-scoped and the audit trail names the real clinician. A service-account
     * implementation may ignore it.
     *
     * @param string $patientUuid the open patient's FHIR uuid (already validated
     *   and authorized at the relay call site)
     * @throws AgentUnavailableException when a token cannot be obtained (the panel
     *   treats "no token to reach the assistant" as "couldn't reach the assistant").
     */
    public function getToken(string $patientUuid): string;
}
