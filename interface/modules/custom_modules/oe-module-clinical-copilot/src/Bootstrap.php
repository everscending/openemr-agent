<?php

/**
 * Clinical Co-Pilot Module Bootstrap Class
 *
 * T016 skeleton. The audit-bridge endpoint (public/audit-bridge.php) is a
 * directly-addressable, self-gating entry point and needs no event
 * subscriptions. subscribeToEvents() is intentionally present and harmless so
 * T017 (citation resolver) and T018 (chart panel) can extend it.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    OpenEMR Clinical Co-Pilot <support@open-emr.org>
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use Symfony\Component\EventDispatcher\EventDispatcherInterface;

class Bootstrap
{
    public const MODULE_INSTALLATION_PATH = "/interface/modules/custom_modules/oe-module-clinical-copilot";
    public const MODULE_NAME = "oe-module-clinical-copilot";

    private readonly EventDispatcherInterface $eventDispatcher;

    public function __construct(EventDispatcherInterface $eventDispatcher)
    {
        $this->eventDispatcher = $eventDispatcher;
    }

    /**
     * Return the dispatcher this module was bootstrapped with. Present so
     * T017/T018 can register listeners; unused in T016.
     */
    public function getEventDispatcher(): EventDispatcherInterface
    {
        return $this->eventDispatcher;
    }

    /**
     * No event subscriptions yet (T016). Left present and harmless for T017/T018.
     */
    public function subscribeToEvents(): void
    {
        // Intentionally empty: the audit-bridge endpoint is a directly-addressable
        // entry point (gated by core when the module is disabled) and does not
        // participate in the UI event system.
    }
}
