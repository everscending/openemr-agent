<?php

/**
 * Clinical Co-Pilot panel -> agent relay endpoint (T021).
 *
 * Unlike T016's audit bridge, this endpoint runs in the LOGGED-IN user's
 * authenticated OpenEMR session: it deliberately does NOT set $ignoreAuth, so
 * core (library/auth.inc.php, included by globals.php) rejects an unauthenticated
 * request before this file's own code ever runs. The controller then CSRF-verifies
 * the POST and independently authorizes the open patient before contacting the
 * agent. No Access-Control-Allow-* header is ever added to OpenEMR — the browser
 * never talks to the agent directly; this same-origin proxy does, server-side.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// phpcs:disable PSR1.Files.SideEffects
$sessionAllowWrite = true;

require_once __DIR__ . "/../../../../globals.php";

use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\ModulesClassLoader;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Modules\ClinicalCopilot\CopilotRelayController;
use OpenEMR\Modules\ClinicalCopilot\HttpCopilotAgentChatClient;
use OpenEMR\Modules\ClinicalCopilot\OAuthServiceTokenProvider;
use OpenEMR\Modules\ClinicalCopilot\SessionPatientAccessGuard;
use Symfony\Component\HttpFoundation\Request;

$classLoader = new ModulesClassLoader(OEGlobalsBag::getInstance()->getProjectDir());
$classLoader->registerNamespaceIfNotExists(
    'OpenEMR\\Modules\\ClinicalCopilot\\',
    dirname(__DIR__) . DIRECTORY_SEPARATOR . 'src'
);

$request = Request::createFromGlobals();
$session = SessionWrapperFactory::getInstance()->getActiveSession();
$request->setSession($session);

$agentBaseUrl = getenv('COPILOT_AGENT_BASE_URL') ?: 'http://copilot:8080';
$oauthBaseUrl = getenv('COPILOT_OAUTH_BASE_URL') ?: 'https://localhost';
$serviceUsername = getenv('COPILOT_SERVICE_USERNAME') ?: 'admin';
$servicePassword = getenv('COPILOT_SERVICE_PASSWORD') ?: 'pass';

$controller = new CopilotRelayController(
    new HttpCopilotAgentChatClient($agentBaseUrl, 120),
    new OAuthServiceTokenProvider($oauthBaseUrl, $serviceUsername, $servicePassword),
    new SessionPatientAccessGuard(),
    ServiceContainer::getLogger(),
);

$response = $controller->handle($request, $session);
$response->send();
