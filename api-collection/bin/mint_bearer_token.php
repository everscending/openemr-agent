<?php

/**
 * One-off bearer-token minting helper for the T031 Bruno API collection.
 *
 * The collection's committed environments (environments/local.bru,
 * environments/railway.bru) deliberately carry NO real token — only the
 * placeholders `REPLACE_WITH_BEARER_TOKEN` / `REPLACE_WITH_BEARER_TOKEN_SHA256`.
 * This script performs the same OAuth2 password-grant flow the co-pilot
 * relay uses (see OAuthServiceTokenProvider, T021) against the `admin`
 * OpenEMR user and prints the two values a grader pastes into their
 * environment (or passes via `bru run --env-var`), never committing them.
 *
 * It does not touch any endpoint under test: it only obtains credentials to
 * call them, the same way a human would grab a token from the OpenEMR UI.
 *
 * Usage (run inside the openemr container as the apache user):
 *
 *   openemr-cmd e "su -s /bin/sh apache -c 'php \
 *     /var/www/localhost/htdocs/openemr/api-collection/bin/mint_bearer_token.php'"
 *
 * Prints two lines, shell-sourceable:
 *   BEARER_TOKEN=<jwt>
 *   BEARER_TOKEN_SHA256=<sha256 hex digest of the jwt>
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

require_once __DIR__ . "/../../interface/globals.php";

use OpenEMR\Core\ModulesClassLoader;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Modules\ClinicalCopilot\OAuthServiceTokenProvider;

$classLoader = new ModulesClassLoader(OEGlobalsBag::getInstance()->getProjectDir());
$classLoader->registerNamespaceIfNotExists(
    'OpenEMR\\Modules\\ClinicalCopilot\\',
    __DIR__ . '/../../interface/modules/custom_modules/oe-module-clinical-copilot/src'
);

$oauthBaseUrl = getenv('COPILOT_OAUTH_BASE_URL') ?: 'https://localhost';
$username = getenv('COPILOT_SERVICE_USERNAME') ?: 'admin';
$password = getenv('COPILOT_SERVICE_PASSWORD') ?: 'pass';

$provider = new OAuthServiceTokenProvider($oauthBaseUrl, $username, $password);
$token = $provider->getToken();

fwrite(STDOUT, 'BEARER_TOKEN=' . $token . "\n");
fwrite(STDOUT, 'BEARER_TOKEN_SHA256=' . hash('sha256', $token) . "\n");
