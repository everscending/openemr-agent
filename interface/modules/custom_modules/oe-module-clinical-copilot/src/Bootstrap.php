<?php

/**
 * Clinical Co-Pilot Module Bootstrap Class
 *
 * T016 added the audit-bridge endpoint (public/audit-bridge.php), a
 * directly-addressable, self-gating entry point that needs no event
 * subscriptions of its own.
 *
 * T019 adds the Dashboard panel scaffolding: a listener on the patient
 * Dashboard's RenderEvent that echoes a static, self-contained asset bundle
 * (CSS + an inert <template> holding the panel markup + JS) into the page.
 * The echoed JS does all the real work client-side -- wrapping
 * #container_div's existing children into a left column and mounting the
 * panel as a new right column -- entirely without any core file edit.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    OpenEMR Clinical Co-Pilot <support@open-emr.org>
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use OpenEMR\Events\PatientDemographics\RenderEvent;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

class Bootstrap
{
    public const MODULE_INSTALLATION_PATH = "/interface/modules/custom_modules/oe-module-clinical-copilot";
    public const MODULE_NAME = "oe-module-clinical-copilot";

    private readonly EventDispatcherInterface $eventDispatcher;

    /** Absolute path to this module's public/ directory. */
    private readonly string $publicDirectory;

    public function __construct(EventDispatcherInterface $eventDispatcher)
    {
        $this->eventDispatcher = $eventDispatcher;
        $this->publicDirectory = dirname(__DIR__) . '/public';
    }

    /**
     * Return the dispatcher this module was bootstrapped with. Present so
     * future tickets can register additional listeners.
     */
    public function getEventDispatcher(): EventDispatcherInterface
    {
        return $this->eventDispatcher;
    }

    /**
     * T016's audit-bridge endpoint is a directly-addressable entry point
     * (gated by core when the module is disabled) and does not participate
     * in the UI event system, so it needs no listener here. T019 adds the
     * Dashboard panel render listener below.
     */
    public function subscribeToEvents(): void
    {
        $this->eventDispatcher->addListener(
            RenderEvent::EVENT_SECTION_LIST_RENDER_AFTER,
            $this->renderCopilotPanel(...)
        );
    }

    /**
     * Echo the co-pilot panel's static asset bundle (CSS + markup template +
     * mounting JS) into the patient Dashboard page. Fails silently (never
     * breaks the Dashboard) if the patient id is not a scalar we can safely
     * encode, or if any of the on-disk asset files are unreadable.
     */
    public function renderCopilotPanel(RenderEvent $event): void
    {
        $pid = $event->getPid();
        if (!is_int($pid)) {
            return;
        }

        $cssPath = $this->publicDirectory . '/copilot-panel.css';
        $htmlPath = $this->publicDirectory . '/copilot-panel.html';
        $jsPath = $this->publicDirectory . '/copilot-panel.js';

        if (!is_readable($cssPath) || !is_readable($htmlPath) || !is_readable($jsPath)) {
            return;
        }

        $css = file_get_contents($cssPath);
        $html = file_get_contents($htmlPath);
        $js = file_get_contents($jsPath);
        if ($css === false || $html === false || $js === false) {
            return;
        }

        // Safely encode the pid for the browser: never raw-interpolated.
        $encodedPid = json_encode((string) $pid);
        if ($encodedPid === false) {
            return;
        }

        echo '<style>' . $css . '</style>';
        // An inert <template> -- not part of the live DOM until the echoed
        // script below clones it -- so even a duplicate echo (double
        // render-event fire) never creates a duplicate live #copilot-panel;
        // the script's own idempotency guard handles that.
        echo '<template class="oe-copilot-panel-source">' . $html . '</template>';
        echo '<script>';
        echo 'window.OE_COPILOT_PID = ' . $encodedPid . ';';
        echo $js;
        echo '</script>';
    }
}
