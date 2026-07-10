<?php

/**
 * Clinical Co-Pilot Dashboard panel scaffolding E2E test (T019).
 *
 * Drives a real logged-in browser session against the patient Dashboard tab
 * (interface/patient_file/summary/demographics.php) to verify the client-side
 * right-hand co-pilot column: presence/absence gated by the module's
 * mod_active flag, static markup, layout, tab-scoping, idempotent mounting,
 * and the no-network text echo (including its textContent-only, never
 * innerHTML, injection-safety guarantee).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\E2e;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Tests\E2e\Base\BaseTrait;
use OpenEMR\Tests\E2e\Login\LoginTestData;
use OpenEMR\Tests\E2e\Login\LoginTrait;
use PHPUnit\Framework\Attributes\Test;
use Symfony\Component\Panther\PantherTestCase;

class ClinicalCopilotDashboardPanelTest extends PantherTestCase
{
    use BaseTrait;
    use LoginTrait;

    private $crawler;

    private const MODULE_DIRECTORY = 'oe-module-clinical-copilot';

    /** Stable dev-fixture patient named in the T019 ticket notes. */
    private const FIXTURE_PID = 100;

    private ?int $originalModActive = null;

    protected function setUp(): void
    {
        parent::setUp();

        $patientRow = QueryUtils::querySingleRow(
            'SELECT `pid` FROM `patient_data` WHERE `pid` = ?',
            [self::FIXTURE_PID]
        );
        if (!is_array($patientRow)) {
            $this->fail('Expected fixture patient pid=' . self::FIXTURE_PID . ' to exist for this test.');
        }

        $modRow = QueryUtils::querySingleRow(
            'SELECT `mod_active` FROM `modules` WHERE `mod_directory` = ?',
            [self::MODULE_DIRECTORY]
        );
        $this->originalModActive = (is_array($modRow) && isset($modRow['mod_active']))
            ? (int) $modRow['mod_active']
            : null;

        // Deterministic starting state for every test: module active.
        $this->setModuleActive(1);
    }

    protected function tearDown(): void
    {
        $this->setModuleActive($this->originalModActive ?? 1);
        parent::tearDown();
    }

    private function setModuleActive(int $active): void
    {
        QueryUtils::sqlStatementThrowException(
            'UPDATE `modules` SET `mod_active` = ? WHERE `mod_directory` = ?',
            [$active, self::MODULE_DIRECTORY]
        );
    }

    private function openDashboard(): void
    {
        $this->client->request(
            'GET',
            '/interface/patient_file/summary/demographics.php?set_pid=' . self::FIXTURE_PID
        );
        $this->client->wait(20)->until(
            fn($driver) => (bool) $driver->executeScript(
                'return document.getElementById("container_div") !== null;'
            )
        );
    }

    /**
     * @return array<string, mixed>
     */
    private function readDashboardState(): array
    {
        $json = (string) $this->client->executeScript(<<<'JS_WRAP'
            var result = {
                panelCount: document.querySelectorAll('#copilot-panel').length,
                leftWrapperCount: document.querySelectorAll('.oe-copilot__dashboard-left').length,
                hasAllergies: document.body.innerText.indexOf('Allergies') !== -1,
                hasPatientPortal: document.body.innerText.indexOf('Patient Portal') !== -1,
                headerText: null,
                messageListEmpty: null,
                messageListOverflowY: null,
                hasInput: document.getElementById('copilot-input') !== null,
                hasSendButton: document.getElementById('copilot-send') !== null,
                containerDisplay: null,
                leftRect: null,
                panelRect: null
            };

            var containerDiv = document.getElementById('container_div');
            if (containerDiv) {
                result.containerDisplay = getComputedStyle(containerDiv).display;
            }

            var panel = document.getElementById('copilot-panel');
            if (panel) {
                var header = panel.querySelector('.card-title, h5, h4, h3');
                result.headerText = header ? header.textContent.trim() : null;
                var list = document.getElementById('copilot-message-list');
                if (list) {
                    result.messageListEmpty = (list.children.length === 0);
                    result.messageListOverflowY = getComputedStyle(list).overflowY;
                }
                result.panelRect = panel.getBoundingClientRect();
            }

            var leftWrapper = document.querySelector('.oe-copilot__dashboard-left');
            if (leftWrapper) {
                result.leftRect = leftWrapper.getBoundingClientRect();
            }

            return JSON.stringify(result);
        JS_WRAP);

        /** @var array<string, mixed> $decoded */
        $decoded = json_decode($json, true);
        $this->assertIsArray($decoded, 'Expected dashboard-state script to return a JSON object.');
        return $decoded;
    }

    // ---------------------------------------------------- criteria 1, 3, 5

    #[Test]
    public function testPanelPresentIntactAndRightColumnWhenActiveAbsentAndFullWidthWhenDisabled(): void
    {
        $this->base();
        try {
            $this->login(LoginTestData::username, LoginTestData::password);

            // ---- module active: panel renders, dashboard intact (criteria 1, 3, 5)
            $this->openDashboard();
            $state = $this->readDashboardState();

            $this->assertSame(1, $state['panelCount'], 'Exactly one #copilot-panel must render when the module is active.');
            $this->assertSame('Clinical Co-Pilot', $state['headerText'], 'Panel header must read "Clinical Co-Pilot".');
            $this->assertTrue($state['messageListEmpty'], '#copilot-message-list must start empty.');
            $this->assertNotSame('visible', $state['messageListOverflowY'], 'Message list must be scrollable, not overflow:visible.');
            $this->assertTrue($state['hasInput'], 'Text input must be present.');
            $this->assertTrue($state['hasSendButton'], 'Send button must be present.');
            $this->assertTrue($state['hasAllergies'], 'Existing dashboard content (Allergies card) must remain present.');
            $this->assertTrue($state['hasPatientPortal'], 'Existing dashboard content (Patient Portal card) must remain present.');
            $this->assertSame('flex', $state['containerDisplay'], '#container_div must become a two-column flex container.');

            $this->assertIsArray($state['leftRect']);
            $this->assertIsArray($state['panelRect']);
            $leftRect = $state['leftRect'];
            $panelRect = $state['panelRect'];

            // Property, not proxy: assert visual right-of geometry, not DOM order.
            $epsilon = 2.0;
            $this->assertGreaterThanOrEqual(
                $leftRect['right'] - $epsilon,
                $panelRect['left'],
                'Co-pilot panel must be positioned visually to the right of the dashboard left column.'
            );
            $this->assertGreaterThan(0.0, $leftRect['width'], 'Left column must have nonzero width.');
            $this->assertGreaterThan(0.0, $panelRect['width'], 'Panel column must have nonzero width.');

            // Loose geometry (not pixel-perfect): left column noticeably larger,
            // roughly targeting the ~63/37 split, within a generous band.
            $totalWidth = $leftRect['width'] + $panelRect['width'];
            $leftRatio = $leftRect['width'] / $totalWidth;
            $this->assertGreaterThan(0.5, $leftRatio, 'Left column should occupy more width than the panel.');
            $this->assertLessThan(0.85, $leftRatio, 'Panel must retain meaningful width, not be squeezed to nothing.');

            // ---- idempotency: re-invoking the mount seam must not duplicate anything
            $recheckJson = (string) $this->client->executeScript(<<<'JS_WRAP'
                if (typeof window.oeCopilotMount !== 'function') {
                    return JSON.stringify({ mountFnExists: false });
                }
                window.oeCopilotMount();
                window.oeCopilotMount();
                return JSON.stringify({
                    mountFnExists: true,
                    panelCount: document.querySelectorAll('#copilot-panel').length,
                    leftWrapperCount: document.querySelectorAll('.oe-copilot__dashboard-left').length
                });
            JS_WRAP);
            /** @var array<string, mixed> $recheck */
            $recheck = json_decode($recheckJson, true);
            $this->assertIsArray($recheck);
            $this->assertTrue($recheck['mountFnExists'], 'Mount seam must expose window.oeCopilotMount for re-invocation.');
            $this->assertSame(1, $recheck['panelCount'], 'Re-invoking mount twice more must still leave exactly one panel.');
            $this->assertSame(1, $recheck['leftWrapperCount'], 'Re-invoking mount twice more must not re-wrap the dashboard.');

            // ---- module disabled: panel absent, dashboard unchanged/full-width (criterion 1)
            $this->setModuleActive(0);
            $this->openDashboard();
            $disabledState = $this->readDashboardState();

            $this->assertSame(0, $disabledState['panelCount'], 'Panel must be entirely absent when the module is disabled.');
            $this->assertSame(0, $disabledState['leftWrapperCount'], 'Dashboard must not be split into columns when disabled.');
            $this->assertTrue($disabledState['hasAllergies'], 'Dashboard content must remain present (unchanged) when disabled.');
            $this->assertTrue($disabledState['hasPatientPortal'], 'Dashboard content must remain present (unchanged) when disabled.');
            $this->assertNotSame('flex', $disabledState['containerDisplay'], '#container_div must not be a flex split container when disabled.');

            $this->setModuleActive(1);
        } catch (\Throwable $e) {
            $this->client->quit();
            throw $e;
        }
        $this->client->quit();
    }

    // ---------------------------------------------------------- criterion 6

    #[Test]
    public function testPanelAbsentOnHistoryTab(): void
    {
        $this->base();
        try {
            $this->login(LoginTestData::username, LoginTestData::password);

            // Precondition (not tautological): confirm the panel DOES render on
            // the Dashboard first, in this same session, so this test cannot
            // pass against an implementation that never renders the panel at
            // all -- only against one that (correctly) scopes it to Dashboard.
            $this->openDashboard();
            $dashboardPanelCount = (int) $this->client->executeScript(
                'return document.querySelectorAll("#copilot-panel").length;'
            );
            $this->assertSame(1, $dashboardPanelCount, 'Precondition failed: panel must render on the Dashboard tab.');

            $this->client->request('GET', '/interface/patient_file/history/history.php');
            $this->client->wait(20)->until(
                fn($driver) => (bool) $driver->executeScript('return document.readyState === "complete";')
            );

            $panelCount = (int) $this->client->executeScript(
                'return document.querySelectorAll("#copilot-panel").length;'
            );
            $this->assertSame(0, $panelCount, 'Co-pilot panel must not render on the History tab.');
        } catch (\Throwable $e) {
            $this->client->quit();
            throw $e;
        }
        $this->client->quit();
    }

    // ---------------------------------------------------------- criterion 4

    #[Test]
    public function testEchoAppendsSanitizedTextOnSendAndEnterButNeverForEmptyOrWhitespace(): void
    {
        $this->base();
        try {
            $this->login(LoginTestData::username, LoginTestData::password);
            $this->openDashboard();

            // Adversarial: whitespace-only input appends nothing.
            $whitespaceResult = $this->sendViaButton('   ');
            $this->assertSame(0, $whitespaceResult['count'], 'Whitespace-only input must append nothing.');
            $this->assertSame(0, $whitespaceResult['networkCalls'], 'No network calls, ever.');

            // Adversarial: empty input appends nothing.
            $emptyResult = $this->sendViaButton('');
            $this->assertSame(0, $emptyResult['count'], 'Empty input must append nothing.');

            // Happy path via Send button click: real text appends, input clears, no network.
            $helloResult = $this->sendViaButton('hello world');
            $this->assertSame(1, $helloResult['count'], 'Sending real text must append exactly one bubble.');
            $this->assertSame('hello world', $helloResult['lastText']);
            $this->assertSame('', $helloResult['inputValueAfter'], 'Input must clear after sending.');
            $this->assertSame(0, $helloResult['networkCalls'], 'Sending must never make a network call.');

            // Happy path via Enter key + injection-safety probe: HTML-look-alike
            // text must render as literal text (textContent), never parsed as markup.
            $htmlLookalike = '<b>bold</b>';
            $enterResult = $this->sendViaEnterKey($htmlLookalike);
            $this->assertSame(2, $enterResult['count'], 'Enter key must append a second bubble.');
            $this->assertSame($htmlLookalike, $enterResult['lastText'], 'Bubble text must be the literal, unescaped-by-us string.');
            $this->assertSame(
                0,
                $enterResult['boldElementCount'],
                'HTML-look-alike input must never be parsed as markup (textContent only, never innerHTML).'
            );
            $this->assertSame(0, $enterResult['networkCalls'], 'Sending via Enter must never make a network call.');
        } catch (\Throwable $e) {
            $this->client->quit();
            throw $e;
        }
        $this->client->quit();
    }

    /**
     * @return array<string, mixed>
     */
    private function sendViaButton(string $text): array
    {
        $json = (string) $this->client->executeScript(<<<JS_WRAP
            var input = document.getElementById('copilot-input');
            var list = document.getElementById('copilot-message-list');
            var sendBtn = document.getElementById('copilot-send');
            if (!input || !list || !sendBtn) {
                return JSON.stringify({ elementsFound: false });
            }

            var calls = 0;
            var origFetch = window.fetch;
            window.fetch = function () { calls++; return origFetch.apply(this, arguments); };
            var OrigXHR = window.XMLHttpRequest;
            var origOpen = OrigXHR.prototype.open;
            OrigXHR.prototype.open = function () { calls++; return origOpen.apply(this, arguments); };

            input.value = {$this->jsStringLiteral($text)};
            sendBtn.click();

            OrigXHR.prototype.open = origOpen;
            window.fetch = origFetch;

            var last = list.lastElementChild;
            return JSON.stringify({
                elementsFound: true,
                count: list.children.length,
                lastText: last ? last.textContent : null,
                inputValueAfter: input.value,
                boldElementCount: list.querySelectorAll('b').length,
                networkCalls: calls
            });
        JS_WRAP);

        /** @var array<string, mixed> $decoded */
        $decoded = json_decode($json, true);
        $this->assertIsArray($decoded, 'Expected send-via-button script to return a JSON object.');
        $this->assertTrue(
            $decoded['elementsFound'] ?? false,
            'Panel input/message-list/send button must exist in the DOM.'
        );
        return $decoded;
    }

    /**
     * @return array<string, mixed>
     */
    private function sendViaEnterKey(string $text): array
    {
        $json = (string) $this->client->executeScript(<<<JS_WRAP
            var input = document.getElementById('copilot-input');
            var list = document.getElementById('copilot-message-list');
            if (!input || !list) {
                return JSON.stringify({ elementsFound: false });
            }

            var calls = 0;
            var origFetch = window.fetch;
            window.fetch = function () { calls++; return origFetch.apply(this, arguments); };
            var OrigXHR = window.XMLHttpRequest;
            var origOpen = OrigXHR.prototype.open;
            OrigXHR.prototype.open = function () { calls++; return origOpen.apply(this, arguments); };

            input.value = {$this->jsStringLiteral($text)};
            var evt = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
            input.dispatchEvent(evt);

            OrigXHR.prototype.open = origOpen;
            window.fetch = origFetch;

            var last = list.lastElementChild;
            return JSON.stringify({
                elementsFound: true,
                count: list.children.length,
                lastText: last ? last.textContent : null,
                inputValueAfter: input.value,
                boldElementCount: list.querySelectorAll('b').length,
                networkCalls: calls
            });
        JS_WRAP);

        /** @var array<string, mixed> $decoded */
        $decoded = json_decode($json, true);
        $this->assertIsArray($decoded, 'Expected send-via-enter script to return a JSON object.');
        $this->assertTrue(
            $decoded['elementsFound'] ?? false,
            'Panel input/message-list must exist in the DOM.'
        );
        return $decoded;
    }

    /**
     * Safely embed an arbitrary PHP string as a JS string literal inside an
     * inline executeScript() heredoc (which is itself PHP-interpolated).
     */
    private function jsStringLiteral(string $value): string
    {
        return json_encode($value, JSON_THROW_ON_ERROR);
    }
}
