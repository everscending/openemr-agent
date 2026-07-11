/**
 * @jest-environment jsdom
 */

/**
 * Tests for citation chip rendering (T018) in
 * public/copilot-panel.js.
 *
 * Split 2026-07-11: this ticket is render-only. `[ResourceType/id]` tokens
 * in an agent reply become non-interactive citation chips built from the
 * reply text plus whatever display data (if any) the relay/ChatResponse
 * payload carries per ref -- fetching nothing, navigating nowhere.
 *
 * Run with: npx jest interface/modules/custom_modules/oe-module-clinical-copilot/tests/js/citation-chips.test.js
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

const fs = require('fs');
const path = require('path');

const MODULE_PATH = path.resolve(__dirname, '../../public/copilot-panel.js');

const {
    parseReplySegments,
    buildAgentReplyFragment,
    KNOWN_RESOURCE_TYPES,
    humanizeResourceType,
    shortenResourceId
} = require(MODULE_PATH);

function fragmentToContainer(fragment) {
    const container = document.createElement('div');
    container.appendChild(fragment);
    return container;
}

// ---------------------------------------------------------------------------
// Criterion 1: known citation tokens become chips (full detail + fallback)
// ---------------------------------------------------------------------------
describe('citation chips -- rendering (criterion 1)', () => {

    test('a known-type token with display+date in refs renders a chip carrying type + identifier + date, replacing the raw token', () => {
        const reply = 'Metformin dose per [MedicationRequest/mr-1].';
        const refs = {
            'MedicationRequest/mr-1': { display: 'Metformin 500mg', date: '2024-01-15' }
        };

        const fragment = buildAgentReplyFragment(document, reply, refs);
        const container = fragmentToContainer(fragment);

        const chips = container.querySelectorAll('.oe-copilot__citation-chip');
        expect(chips.length).toBe(1);
        expect(chips[0].textContent).toBe('Medication Request: Metformin 500mg (2024-01-15)');

        // Raw token must be gone from the rendered text -- it was replaced,
        // not merely decorated.
        expect(container.textContent).not.toContain('[MedicationRequest/mr-1]');
        // Surrounding plain text is preserved.
        expect(container.textContent).toContain('Metformin dose per ');
        expect(container.textContent).toContain('.');
    });

    test('a known-type token with NO display detail in refs falls back to type + shortened id, never blocking the render', () => {
        const reply = 'See [Observation/observation-abc123] for detail.';

        // No refs argument at all -- mirrors the current relay payload shape,
        // which carries no per-ref display data.
        const fragment = buildAgentReplyFragment(document, reply, undefined);
        const container = fragmentToContainer(fragment);

        const chips = container.querySelectorAll('.oe-copilot__citation-chip');
        expect(chips.length).toBe(1);
        // "observation-abc123" (19 chars) shortens to first 6 chars + ellipsis.
        expect(chips[0].textContent).toBe('Observation #observ…');
        expect(container.textContent).not.toContain('[Observation/observation-abc123]');
    });

    test('refs present but missing an entry for this specific token still falls back to id shortening (never throws)', () => {
        const reply = '[Condition/cond-1]';
        const refs = { 'Condition/some-other-id': { display: 'Irrelevant' } };

        expect(() => buildAgentReplyFragment(document, reply, refs)).not.toThrow();
        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, refs));
        const chip = container.querySelector('.oe-copilot__citation-chip');
        expect(chip).not.toBeNull();
        expect(chip.textContent).toBe('Condition #cond-1');
    });

    test('multiple distinct known resource types humanize correctly', () => {
        expect(humanizeResourceType('AllergyIntolerance')).toBe('Allergy Intolerance');
        expect(humanizeResourceType('DocumentReference')).toBe('Document Reference');
        expect(humanizeResourceType('Patient')).toBe('Patient');
    });

    test('short ids (<=8 chars) are not truncated', () => {
        expect(shortenResourceId('mr-1')).toBe('mr-1');
        expect(shortenResourceId('12345678')).toBe('12345678');
    });

    test('KNOWN_RESOURCE_TYPES matches the agent-side closed set', () => {
        expect(KNOWN_RESOURCE_TYPES.sort()).toEqual([
            'AllergyIntolerance',
            'Condition',
            'DocumentReference',
            'Encounter',
            'Immunization',
            'MedicationRequest',
            'Observation',
            'Patient'
        ].sort());
    });
});

// ---------------------------------------------------------------------------
// Criterion 2: malformed / unknown tokens are a soft failure
// ---------------------------------------------------------------------------
describe('citation chips -- soft failure on malformed/unknown tokens (criterion 2)', () => {

    test('a resource type outside the known set renders as unchanged plain text, no chip, no throw', () => {
        const reply = 'Unrelated tag [Foo/1] appears here.';

        expect(() => buildAgentReplyFragment(document, reply, undefined)).not.toThrow();
        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, undefined));

        expect(container.querySelectorAll('.oe-copilot__citation-chip').length).toBe(0);
        expect(container.textContent).toBe('Unrelated tag [Foo/1] appears here.');
    });

    test('a near-miss on case ("observation" lowercase) is treated as unknown, not matched loosely', () => {
        const reply = 'ref [observation/obs-1] here';
        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, undefined));

        expect(container.querySelectorAll('.oe-copilot__citation-chip').length).toBe(0);
        expect(container.textContent).toBe('ref [observation/obs-1] here');
    });

    test('a structurally malformed token (no slash) is left as unchanged plain text', () => {
        const reply = 'broken [Observation] token';
        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, undefined));

        expect(container.querySelectorAll('.oe-copilot__citation-chip').length).toBe(0);
        expect(container.textContent).toBe('broken [Observation] token');
    });

    test('an unterminated bracket (no closing "]") is left as unchanged plain text, no throw', () => {
        const reply = 'oops [Observation/obs-1 missing close';
        expect(() => buildAgentReplyFragment(document, reply, undefined)).not.toThrow();
        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, undefined));
        expect(container.textContent).toBe('oops [Observation/obs-1 missing close');
    });

    test('an empty resource id ("[Observation/]") is left as unchanged plain text', () => {
        const reply = 'weird [Observation/] token';
        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, undefined));
        expect(container.querySelectorAll('.oe-copilot__citation-chip').length).toBe(0);
        expect(container.textContent).toBe('weird [Observation/] token');
    });

    test('a completely non-string reply never throws and produces no chip', () => {
        expect(() => buildAgentReplyFragment(document, null, undefined)).not.toThrow();
        expect(() => buildAgentReplyFragment(document, undefined, undefined)).not.toThrow();
        const container = fragmentToContainer(buildAgentReplyFragment(document, null, undefined));
        expect(container.querySelectorAll('.oe-copilot__citation-chip').length).toBe(0);
    });
});

// ---------------------------------------------------------------------------
// Criterion 3: chips are inert -- no network, no navigation on click
// ---------------------------------------------------------------------------
describe('citation chips -- inertness (criterion 3)', () => {

    test('clicking a chip triggers no network request and no navigation', () => {
        const reply = '[Encounter/enc-1]';
        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, undefined));
        document.body.appendChild(container);

        const originalFetch = global.fetch;
        global.fetch = jest.fn();
        const locationBefore = window.location.href;

        const chip = container.querySelector('.oe-copilot__citation-chip');
        expect(chip).not.toBeNull();
        expect(chip.onclick).toBeNull();

        chip.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));

        expect(global.fetch).not.toHaveBeenCalled();
        expect(window.location.href).toBe(locationBefore);

        global.fetch = originalFetch;
        document.body.removeChild(container);
    });
});

// ---------------------------------------------------------------------------
// Criterion 4: never innerHTML with reply-derived content; markup is inert
// ---------------------------------------------------------------------------
describe('citation chips -- untrusted input discipline (criterion 4)', () => {

    test('reply-borne markup never becomes a DOM element -- renders as inert literal text', () => {
        window.__pwned = false;
        const reply = 'Alert: <img src=x onerror="window.__pwned=true"> ref [Observation/obs-9]';

        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, undefined));
        document.body.appendChild(container);

        // No element was ever parsed out of the reply text.
        expect(container.querySelector('img')).toBeNull();
        // The markup survives only as inert visible text.
        expect(container.textContent).toContain('<img src=x onerror="window.__pwned=true">');
        // It was never executed.
        expect(window.__pwned).toBe(false);
        // The valid citation token alongside it still renders as a chip.
        const chip = container.querySelector('.oe-copilot__citation-chip');
        expect(chip).not.toBeNull();
        expect(chip.textContent).toBe('Observation #obs-9');

        document.body.removeChild(container);
        delete window.__pwned;
    });

    test('the module source never assigns/reads .innerHTML anywhere (comments mentioning the word are fine)', () => {
        const source = fs.readFileSync(MODULE_PATH, 'utf8');
        expect(source).not.toMatch(/\.innerHTML\b/);
    });

    test('chip building assigns textContent, never innerHTML, even for a hostile display string in refs', () => {
        const reply = '[Patient/p-1]';
        const refs = {
            'Patient/p-1': { display: '<script>window.__pwned2=true</script>', date: '2024-01-01' }
        };
        window.__pwned2 = false;

        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, refs));
        document.body.appendChild(container);

        expect(container.querySelector('script')).toBeNull();
        expect(window.__pwned2).toBe(false);
        const chip = container.querySelector('.oe-copilot__citation-chip');
        expect(chip.textContent).toBe('Patient: <script>window.__pwned2=true</script> (2024-01-01)');

        document.body.removeChild(container);
        delete window.__pwned2;
    });
});

// ---------------------------------------------------------------------------
// Additional adversarial probes beyond the ticket's own examples
// ---------------------------------------------------------------------------
describe('citation chips -- additional adversarial probes', () => {

    test('a token immediately adjacent to punctuation (no surrounding space) still parses', () => {
        const reply = 'Confirmed([Immunization/imm-7]).';
        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, undefined));
        const chip = container.querySelector('.oe-copilot__citation-chip');
        expect(chip).not.toBeNull();
        expect(container.textContent).toBe('Confirmed(Immunization #imm-7).');
    });

    test('a reply mixing a known token, an unknown-type token, and plain text renders exactly one chip', () => {
        const reply = 'Known [DocumentReference/doc-1] and unknown [Bogus/xyz] and plain text.';
        const container = fragmentToContainer(buildAgentReplyFragment(document, reply, undefined));
        expect(container.querySelectorAll('.oe-copilot__citation-chip').length).toBe(1);
        expect(container.textContent).toContain('[Bogus/xyz]');
        expect(container.textContent).not.toContain('[DocumentReference/doc-1]');
    });

    test('parseReplySegments is a pure function returning a segment list (no DOM dependency)', () => {
        const segments = parseReplySegments('a [Patient/p-1] b');
        expect(segments).toEqual([
            { kind: 'text', value: 'a ' },
            { kind: 'citation', resourceType: 'Patient', resourceId: 'p-1', token: '[Patient/p-1]' },
            { kind: 'text', value: ' b' }
        ]);
    });
});
