<?php

/**
 * Clinical Co-Pilot — Laminas Module Manager action listener.
 *
 * Reports install / enable / disable actions back to the Module Manager. The
 * module owns no schema of its own (it writes existing audit tables), so the
 * only state it maintains is the modules-table active flag that the
 * audit-bridge endpoint gates on.
 *
 * @package   OpenEMR Modules
 * @link      https://www.open-emr.org
 * @author    OpenEMR Clinical Co-Pilot <support@open-emr.org>
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Core\AbstractModuleActionListener;

class ModuleManagerListener extends AbstractModuleActionListener
{
    public function __construct()
    {
        parent::__construct();
    }

    /**
     * @param string $methodName
     * @param int|string $modId
     * @param string $currentActionStatus
     * @return string
     */
    public function moduleManagerAction($methodName, $modId, string $currentActionStatus = 'Success'): string
    {
        if (method_exists(self::class, $methodName)) {
            // A variable-variable static call is mixed to PHPStan; narrow the
            // result rather than cast. The action methods all return string, so
            // the fallback is defensive only.
            $result = self::$methodName($modId, $currentActionStatus);
            return is_string($result) ? $result : $currentActionStatus;
        }
        return $currentActionStatus;
    }

    public static function getModuleNamespace(): string
    {
        return 'OpenEMR\\Modules\\ClinicalCopilot\\';
    }

    public static function initListenerSelf(): ModuleManagerListener
    {
        return new self();
    }

    /**
     * @param int|string $modId
     * @param string $currentActionStatus
     */
    private function install($modId, string $currentActionStatus): string
    {
        // Show the config button before enable.
        self::setModuleState($modId, '0', '1');
        return $currentActionStatus;
    }

    /**
     * @param int|string $modId
     * @param string $currentActionStatus
     */
    private function enable($modId, string $currentActionStatus): string
    {
        self::setModuleState($modId, '1', '0');
        return $currentActionStatus;
    }

    /**
     * @param int|string $modId
     * @param string $currentActionStatus
     */
    private function disable($modId, string $currentActionStatus): string
    {
        self::setModuleState($modId, '0', '1');
        return $currentActionStatus;
    }

    /**
     * @param int|string $modId
     * @param string $currentActionStatus
     */
    private function unregister($modId, string $currentActionStatus): string
    {
        return $currentActionStatus;
    }

    /**
     * @param int|string $modId module id or directory name
     * @param int|string $flag 1 or 0 to activate or deactivate module.
     * @param int|string $flagUi custom flag to activate or deactivate Manager UI button states.
     */
    private static function setModuleState(int|string $modId, int|string $flag, int|string $flagUi): void
    {
        QueryUtils::sqlStatementThrowException(
            "UPDATE `modules` SET `mod_active` = ?, `mod_ui_active` = ? WHERE `mod_id` = ? OR `mod_directory` = ?",
            [$flag, $flagUi, $modId, $modId]
        );
    }
}
