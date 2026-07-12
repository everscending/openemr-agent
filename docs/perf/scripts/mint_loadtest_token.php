<?php

/**
 * T030 load-test token minting helper.
 *
 * Mints ONE real, T027-shaped FHIR bearer for a given clinician + patient,
 * using the actual production `SmartLaunchTokenProvider` (not a
 * reimplementation) so the token exercises the exact same guardrails,
 * scopes, and audit attribution a real relay-issued token would. Prints the
 * bearer to stdout and nothing else — callers capture stdout only.
 *
 * Deliberately mints ONE token, reused by every virtual user in a load-test
 * run: T040 requires a follow-up turn to reuse the same token (its hash is
 * the conversation's caller-identity binding), and this is simplest way to
 * guarantee that property holds for every virtual user's own follow-up
 * without minting-under-load. Concurrent virtual users sharing one bearer
 * models one clinician with several concurrent conversations open — a
 * reasonable stand-in for load purposes, not a security concern (this
 * mints against a local/demo patient for a load test, never in a request
 * path).
 *
 * Usage (run inside the openemr container, web-user context — see
 * docs/perf/README.md "Reproducing a run"):
 *
 *   php docs/perf/scripts/mint_loadtest_token.php <clinician_user_id> <patient_uuid>
 *
 * Never commit the printed token. It is a real, short-lived (1h) bearer.
 */

declare(strict_types=1);

// phpcs:disable PSR1.Files.SideEffects
$ignoreAuth = true;
$sessionAllowWrite = true;
$_SERVER['HTTP_HOST'] ??= 'localhost';
$_SERVER['SERVER_NAME'] ??= 'localhost';
$_SERVER['REMOTE_ADDR'] ??= '127.0.0.1';
$_GET['site'] ??= 'default';

require_once __DIR__ . '/../../../interface/globals.php';

use OpenEMR\Modules\ClinicalCopilot\SmartLaunchTokenProvider;

$clinicianUserId = isset($argv[1]) ? (int)$argv[1] : 0;
$patientUuid = $argv[2] ?? '';

if ($clinicianUserId <= 0 || $patientUuid === '') {
    fwrite(STDERR, "usage: mint_loadtest_token.php <clinician_user_id> <patient_uuid>\n");
    exit(2);
}

$provider = new SmartLaunchTokenProvider($clinicianUserId);

try {
    $token = $provider->getToken($patientUuid);
} catch (\Throwable $e) {
    fwrite(STDERR, 'mint failed: ' . get_class($e) . "\n");
    exit(1);
}

// stdout: the bearer, and only the bearer.
fwrite(STDOUT, $token);
