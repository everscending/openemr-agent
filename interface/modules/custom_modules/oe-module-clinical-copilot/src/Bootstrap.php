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

use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Events\PatientDemographics\RenderEvent;
use RuntimeException;
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

        // T021: the panel's relay call needs a per-session CSRF token, the
        // patient's FHIR uuid (what the agent's FHIR tools key on), and the
        // same-origin relay URL. All are computed defensively — any failure
        // degrades to empty strings (the client then shows the panel as
        // unavailable) and never breaks the Dashboard.
        [$csrfToken, $patientUuid] = $this->relayClientContext($pid);
        $relayUrl = $this->relayUrl();

        $encodedCsrf = json_encode($csrfToken);
        $encodedUuid = json_encode($patientUuid);
        $encodedRelayUrl = json_encode($relayUrl);
        if ($encodedCsrf === false || $encodedUuid === false || $encodedRelayUrl === false) {
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
        echo 'window.OE_COPILOT_CSRF = ' . $encodedCsrf . ';';
        echo 'window.OE_COPILOT_PATIENT_UUID = ' . $encodedUuid . ';';
        echo 'window.OE_COPILOT_RELAY_URL = ' . $encodedRelayUrl . ';';
        echo $js;
        echo '</script>';
    }

    /**
     * Resolve the per-session CSRF token and the patient's FHIR uuid for the
     * relay POST. Defensive: returns empty strings on any failure so the panel
     * still renders.
     *
     * @return array{0: string, 1: string} [csrfToken, patientUuid]
     */
    private function relayClientContext(int $pid): array
    {
        $csrfToken = '';
        $patientUuid = '';
        try {
            $session = SessionWrapperFactory::getInstance()->getActiveSession();
            $privateKey = $session->get('csrf_private_key', null);
            if ($privateKey !== null && $privateKey !== '') {
                $csrfToken = CsrfUtils::collectCsrfToken($session);
            }

            $row = QueryUtils::querySingleRow(
                'SELECT `uuid` FROM `patient_data` WHERE `pid` = ?',
                [$pid]
            );
            if (is_array($row) && isset($row['uuid']) && is_string($row['uuid']) && $row['uuid'] !== '') {
                $patientUuid = UuidRegistry::uuidToString($row['uuid']);
            }
        } catch (RuntimeException) {
            // A DB or CSRF-key failure must not break the Dashboard; the panel
            // renders and the client reports the assistant as unavailable.
            return [$csrfToken, $patientUuid];
        }

        return [$csrfToken, $patientUuid];
    }

    private function relayUrl(): string
    {
        $webroot = OEGlobalsBag::getInstance()->getString('webroot');
        return $webroot . self::MODULE_INSTALLATION_PATH . '/public/copilot-relay.php';
    }
}
