<?php

/**
 * Clinical Co-Pilot Module Bootstrap
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    OpenEMR Clinical Co-Pilot <support@open-emr.org>
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

use OpenEMR\Core\ModulesClassLoader;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Modules\ClinicalCopilot\Bootstrap;

$file = OEGlobalsBag::getInstance()->getProjectDir();
$classLoader = new ModulesClassLoader($file);
$classLoader->registerNamespaceIfNotExists('OpenEMR\\Modules\\ClinicalCopilot\\', __DIR__ . DIRECTORY_SEPARATOR . 'src');

$eventDispatcher = OEGlobalsBag::getInstance()->getKernel()->getEventDispatcher();
$bootstrap = new Bootstrap($eventDispatcher);
$bootstrap->subscribeToEvents();
