<?php

/**
 * Clinical Co-Pilot — audit-bridge endpoint (T016).
 *
 * The agent service POSTs one invocation record per completed /chat turn here;
 * the module writes it to OpenEMR's decision log and §164.528 disclosure
 * accounting via EventAuditLogger. This is the sole durable audit record that
 * an AI mediated the access (ARCHITECTURE.md §7, :392-395).
 *
 * $ignoreAuth is REQUIRED: without it library/auth.inc.php renders an HTML
 * login page and exits with HTTP 200 for an unauthenticated request, which the
 * agent's fail-open client would read as a successful audit write that never
 * happened. Authentication is instead performed against the presented OAuth2
 * bearer inside AuditBridgeController.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    OpenEMR Clinical Co-Pilot <support@open-emr.org>
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// phpcs:disable PSR1.Files.SideEffects
$ignoreAuth = true;
$sessionAllowWrite = true;

require_once __DIR__ . "/../../../../globals.php";

use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\ModulesClassLoader;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Modules\ClinicalCopilot\AuditBridgeController;
use Symfony\Component\HttpFoundation\Request;

// Register this module's namespace directly so the controller class loads.
// (This line is only ever reached when the module is active: core gates a
// disabled module at globals.php:754-758 — http_response_code(401) + exit(1) —
// before execution reaches here, so there is no "disabled module" case for this
// file to handle.)
$classLoader = new ModulesClassLoader(OEGlobalsBag::getInstance()->getProjectDir());
$classLoader->registerNamespaceIfNotExists(
    'OpenEMR\\Modules\\ClinicalCopilot\\',
    dirname(__DIR__) . DIRECTORY_SEPARATOR . 'src'
);

$request = Request::createFromGlobals();
$session = SessionWrapperFactory::getInstance()->getActiveSession();
$request->setSession($session);

$controller = new AuditBridgeController();
$response = $controller->handle($request, $session);
$response->send();
