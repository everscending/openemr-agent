<?php

/**
 * One-off, idempotent provisioning of the co-pilot relay's service OAuth2 client
 * (T021). Run once per environment (same spirit as T023's modules-row insert):
 *
 *   su -s /bin/sh apache -c 'php interface/modules/custom_modules/oe-module-clinical-copilot/bin/provision_relay_client.php'
 *
 * It registers a single OAuth2 client (grant_types password + refresh_token) for
 * the `admin` service account and enables it (is_enabled=1) — the manual
 * admin-approval step, done headlessly. It is safe to re-run: if an enabled
 * client already exists it reports and exits without creating another. The
 * relay's OAuthServiceTokenProvider performs the same check-then-provision
 * lazily, so this script is a convenience/verification, not a hard prerequisite.
 * Nothing here is committed as config: the enabled client lives in the DB and is
 * reused by marker name.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// phpcs:disable PSR1.Files.SideEffects
$ignoreAuth = true;
$sessionAllowWrite = true;

$_SERVER['HTTP_HOST'] ??= 'localhost';
$_SERVER['SERVER_NAME'] ??= 'localhost';

require_once __DIR__ . "/../../../../globals.php";

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Modules\ClinicalCopilot\OAuthServiceTokenProvider;
use OpenEMR\Core\ModulesClassLoader;
use OpenEMR\Core\OEGlobalsBag;

$classLoader = new ModulesClassLoader(OEGlobalsBag::getInstance()->getProjectDir());
$classLoader->registerNamespaceIfNotExists(
    'OpenEMR\\Modules\\ClinicalCopilot\\',
    dirname(__DIR__) . DIRECTORY_SEPARATOR . 'src'
);

$existing = QueryUtils::fetchRecords(
    'SELECT `client_id` FROM `oauth_clients` WHERE `client_name` = ? AND `is_enabled` = 1',
    [OAuthServiceTokenProvider::CLIENT_NAME]
);
if ($existing !== []) {
    $clientId = is_array($existing[0]) ? ($existing[0]['client_id'] ?? '(unknown)') : '(unknown)';
    fwrite(STDOUT, "Relay service client already provisioned and enabled: {$clientId}\n");
    exit(0);
}

$oauthBaseUrl = getenv('COPILOT_OAUTH_BASE_URL') ?: 'https://localhost';
$username = getenv('COPILOT_SERVICE_USERNAME') ?: 'admin';
$password = getenv('COPILOT_SERVICE_PASSWORD') ?: 'pass';

// Minting a token exercises the full register+enable+grant path idempotently.
$provider = new OAuthServiceTokenProvider($oauthBaseUrl, $username, $password);
$token = $provider->getToken();

$provisioned = QueryUtils::fetchRecords(
    'SELECT `client_id` FROM `oauth_clients` WHERE `client_name` = ? AND `is_enabled` = 1',
    [OAuthServiceTokenProvider::CLIENT_NAME]
);
$clientId = ($provisioned !== [] && is_array($provisioned[0])) ? ($provisioned[0]['client_id'] ?? '(unknown)') : '(unknown)';

fwrite(STDOUT, "Relay service client provisioned and enabled: {$clientId}\n");
fwrite(STDOUT, 'Token grant OK (length ' . strlen($token) . ").\n");
exit(0);
