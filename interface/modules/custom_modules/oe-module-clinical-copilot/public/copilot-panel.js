/**
 * Clinical Co-Pilot Dashboard panel client script (T019).
 *
 * Mount seam: mountCopilotPanel() is the ONLY place that touches
 * #container_div (wrapping its existing children into a left column and
 * appending the panel as the new right column). It is idempotent -- if
 * #copilot-panel already exists in the document, it no-ops -- because the
 * RenderEvent this script is echoed from can, in principle, fire more than
 * once per page load. window.oeCopilotMount exposes it for re-invocation
 * (used by the T019 test suite to prove that idempotency).
 *
 * Echo handling (wireEchoHandler) is deliberately separate from the mount
 * seam: the synchronous echo appends the user's text via textContent only,
 * never innerHTML, so there is no injection surface, and it makes NO network
 * call itself (T019's guarantee). T021 adds a DISTINCT, asynchronous follow-up:
 * after the echo, the user's turn is relayed (deferred to a later task) to the
 * module's same-origin relay endpoint; on success the assistant's reply is
 * rendered as an .oe-copilot__bubble--agent bubble via textContent (an LLM
 * reply must never be parsed as HTML); on failure a fixed, generic "couldn't
 * reach the assistant" status is shown OUTSIDE the message list, leaking no
 * error detail.
 *
 * Citation chips (T018, render-only -- navigation split to T035): a reply's
 * `[ResourceType/id]` tokens are parsed out and rendered as inert
 * `.oe-copilot__citation-chip` spans in place of the raw token. This is
 * parse-and-render only -- no resolver call, no navigation, no extra network
 * request. Today's relay JSON (see CopilotRelayController::handle) carries
 * only { reply, conversation_id, fallback } -- no per-ref display detail --
 * so `refs` below is always empty in production and every chip renders via
 * the "type + shortened id" fallback path; the full-detail path (type +
 * human identifier + date) exists and is exercised by the T018 test suite
 * against a synthetic refs map so it is ready the moment a future ticket
 * adds ref display data to the payload, without requiring a JS change here.
 * Unknown resource types and malformed tokens are a soft failure: the raw
 * text renders unchanged, never a thrown error. Chip content is built via
 * createElement/textContent only, exactly like the echo and agent-reply
 * handling above -- reply text and any ref-derived display strings are
 * untrusted model/tool output and must never reach innerHTML.
 */
(function () {
    // -------------------------------------------------------------------
    // Citation chips (T018) -- pure parsing + safe DOM building, no I/O.
    // -------------------------------------------------------------------

    // The closed set of FHIR resource types the copilot tools read (mirrors
    // agent/src/copilot/contracts/refs.py's FhirResourceType). A token whose
    // type falls outside this set is an unknown/soft-failure case: rendered
    // as unchanged plain text, never a chip, never a thrown error.
    var KNOWN_RESOURCE_TYPES = [
        'Patient',
        'MedicationRequest',
        'Condition',
        'AllergyIntolerance',
        'Observation',
        'Encounter',
        'DocumentReference',
        'Immunization'
    ];

    // Matches `[ResourceType/id]` tokens within arbitrary reply text.
    // Deliberately NOT anchored (^...$) -- it scans for tokens embedded
    // anywhere in a longer string, unlike the parser that validates a single
    // standalone token. An empty id, a missing slash, or an unterminated
    // bracket simply fails to match and is left as ordinary text.
    var CITATION_TOKEN_RE = /\[([A-Za-z]+)\/([^[\]\s/]+)\]/g;

    function isKnownResourceType(resourceType) {
        return KNOWN_RESOURCE_TYPES.indexOf(resourceType) !== -1;
    }

    // "AllergyIntolerance" -> "Allergy Intolerance"; "Patient" -> "Patient".
    function humanizeResourceType(resourceType) {
        return resourceType.replace(/([a-z0-9])([A-Z])/g, '$1 $2');
    }

    // Ids longer than 8 characters shorten to their first 6 chars + an
    // ellipsis, so the fallback chip stays short and legible.
    function shortenResourceId(resourceId) {
        if (resourceId.length <= 8) {
            return resourceId;
        }
        return resourceId.slice(0, 6) + '…';
    }

    // Builds the human-readable chip label. `refInfo` (a { display, date }
    // shape looked up from the caller-supplied refs map) is optional and, in
    // production today, always absent -- see the file-header note. Absent or
    // empty display data degrades to "type + shortened id", never a blocked
    // render.
    function citationChipLabel(resourceType, resourceId, refInfo) {
        var typeLabel = humanizeResourceType(resourceType);
        if (refInfo && typeof refInfo.display === 'string' && refInfo.display !== '') {
            var label = typeLabel + ': ' + refInfo.display;
            if (typeof refInfo.date === 'string' && refInfo.date !== '') {
                label += ' (' + refInfo.date + ')';
            }
            return label;
        }
        return typeLabel + ' #' + shortenResourceId(resourceId);
    }

    // Pure function: splits reply text into an ordered list of plain-text
    // and citation segments. No DOM dependency -- safe to unit test in
    // isolation and safe to call from a non-browser (Node/Jest) context.
    function parseReplySegments(text) {
        var segments = [];
        var lastIndex = 0;
        var match;

        CITATION_TOKEN_RE.lastIndex = 0;
        while ((match = CITATION_TOKEN_RE.exec(text)) !== null) {
            var resourceType = match[1];
            var resourceId = match[2];
            var tokenText = match[0];
            var start = match.index;

            if (start > lastIndex) {
                segments.push({ kind: 'text', value: text.slice(lastIndex, start) });
            }

            if (isKnownResourceType(resourceType)) {
                segments.push({
                    kind: 'citation',
                    resourceType: resourceType,
                    resourceId: resourceId,
                    token: tokenText
                });
            } else {
                // Soft failure: an unknown resource type is not a citation --
                // keep the raw bracketed text exactly as written.
                segments.push({ kind: 'text', value: tokenText });
            }

            lastIndex = start + tokenText.length;
        }

        if (lastIndex < text.length) {
            segments.push({ kind: 'text', value: text.slice(lastIndex) });
        }

        return segments;
    }

    // Builds a DocumentFragment for an agent reply: plain text becomes text
    // nodes, known citation tokens become inert `.oe-copilot__citation-chip`
    // spans. Never uses innerHTML anywhere in this path -- reply text and any
    // ref-derived display strings are untrusted model/tool output, exactly
    // like the echo and agent-bubble handling elsewhere in this file. Any
    // parsing failure or non-string input is a soft failure: the original
    // text (if any) renders unchanged rather than throwing or leaving the
    // bubble blank.
    function buildAgentReplyFragment(doc, text, refs) {
        var fragment = doc.createDocumentFragment();
        if (typeof text !== 'string' || text === '') {
            return fragment;
        }

        var segments;
        try {
            segments = parseReplySegments(text);
        } catch (e) {
            fragment.appendChild(doc.createTextNode(text));
            return fragment;
        }

        segments.forEach(function (segment) {
            if (segment.kind === 'text') {
                if (segment.value !== '') {
                    fragment.appendChild(doc.createTextNode(segment.value));
                }
                return;
            }

            var refInfo = refs && typeof refs === 'object'
                ? refs[segment.resourceType + '/' + segment.resourceId]
                : null;

            var chip = doc.createElement('span');
            // Reuse OpenEMR/Bootstrap's pill styling for the chrome; the
            // BEM class carries this module's own scoping and non-interactive
            // styling (see copilot-panel.css).
            chip.className = 'oe-copilot__citation-chip badge badge-pill badge-light';
            // A tooltip only -- no click handler is ever attached, so a chip
            // triggers no network request and no navigation (criterion 3).
            chip.setAttribute('title', segment.token);
            // textContent only -- never innerHTML -- the label may embed a
            // ref's model/tool-derived display string.
            chip.textContent = citationChipLabel(segment.resourceType, segment.resourceId, refInfo);
            fragment.appendChild(chip);
        });

        return fragment;
    }

    function mountCopilotPanel() {
        if (document.getElementById('copilot-panel')) {
            // Already mounted -- a double render-event fire must be a no-op.
            return;
        }

        var containerDiv = document.getElementById('container_div');
        if (!containerDiv) {
            return;
        }

        var templates = document.querySelectorAll('template.oe-copilot-panel-source');
        if (templates.length === 0) {
            return;
        }

        var template = templates[templates.length - 1];
        var fragment = template.content.cloneNode(true);
        var panel = fragment.querySelector('#copilot-panel');
        if (!panel) {
            return;
        }
        panel.classList.add('oe-copilot__panel');

        var leftColumn = document.createElement('div');
        leftColumn.className = 'oe-copilot__dashboard-left';
        while (containerDiv.firstChild) {
            leftColumn.appendChild(containerDiv.firstChild);
        }

        containerDiv.classList.add('oe-copilot__split-container');
        containerDiv.appendChild(leftColumn);
        containerDiv.appendChild(panel);

        wireEchoHandler(panel);
    }

    function wireEchoHandler(panel) {
        var input = panel.querySelector('#copilot-input');
        var sendBtn = panel.querySelector('#copilot-send');
        var list = panel.querySelector('#copilot-message-list');
        if (!input || !sendBtn || !list) {
            return;
        }

        function sendMessage() {
            var text = input.value.trim();
            if (text === '') {
                // Adversarial: empty/whitespace-only input appends nothing and
                // dispatches no request.
                return;
            }
            var bubble = document.createElement('div');
            bubble.className = 'oe-copilot__bubble oe-copilot__bubble--user';
            // textContent only -- never innerHTML -- no injection surface.
            bubble.textContent = text;
            list.appendChild(bubble);
            list.scrollTop = list.scrollHeight;
            input.value = '';

            // Deferred to a later task so the SYNCHRONOUS echo above makes no
            // network call (optimistic UI); the agent request is a distinct
            // asynchronous follow-up.
            setTimeout(function () {
                sendToAgent(text);
            }, 0);
        }

        function sendToAgent(text) {
            var relayUrl = window.OE_COPILOT_RELAY_URL;
            var csrf = window.OE_COPILOT_CSRF;
            var patientId = window.OE_COPILOT_PATIENT_UUID;
            if (!relayUrl || !csrf || !patientId) {
                showStatus();
                return;
            }

            var params = new URLSearchParams();
            params.set('csrf_token_form', csrf);
            params.set('message', text);
            params.set('patient_id', patientId);
            if (window.OE_COPILOT_CONVERSATION_ID) {
                params.set('conversation_id', window.OE_COPILOT_CONVERSATION_ID);
            }

            clearStatus();
            fetch(relayUrl, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: params.toString()
            }).then(function (response) {
                if (!response.ok) {
                    throw new Error('relay_not_ok');
                }
                return response.json();
            }).then(function (data) {
                if (!data || typeof data.reply !== 'string') {
                    showStatus();
                    return;
                }
                if (typeof data.conversation_id === 'string') {
                    window.OE_COPILOT_CONVERSATION_ID = data.conversation_id;
                }
                appendAgentBubble(data.reply);
            }).catch(function () {
                // Never surface an exception or its message: one fixed, generic
                // state only (criterion 4).
                showStatus();
            });
        }

        function appendAgentBubble(reply, refs) {
            var bubble = document.createElement('div');
            bubble.className = 'oe-copilot__bubble oe-copilot__bubble--agent';
            // Citation-chip rendering (T018): builds the bubble's content from
            // text nodes and inert chip spans via createElement/textContent
            // only -- an LLM reply must never be parsed as markup. `refs` is
            // undefined today (the relay payload carries no per-ref display
            // data yet), so every citation renders via the type+id fallback;
            // see the file-header note.
            bubble.appendChild(buildAgentReplyFragment(document, reply, refs));
            list.appendChild(bubble);
            list.scrollTop = list.scrollHeight;
        }

        function statusElement() {
            var status = document.getElementById('copilot-status');
            if (!status) {
                status = document.createElement('div');
                status.id = 'copilot-status';
                status.className = 'oe-copilot__status';
                status.setAttribute('role', 'status');
                // Deliberately OUTSIDE #copilot-message-list: a connection
                // failure must never add or remove a message bubble.
                var host = panel.querySelector('.oe-copilot__card-body') || panel;
                host.appendChild(status);
            }
            return status;
        }

        function showStatus() {
            statusElement().textContent =
                'Sorry, we couldn’t reach the assistant. Please try again.';
        }

        function clearStatus() {
            var status = document.getElementById('copilot-status');
            if (status) {
                status.textContent = '';
            }
        }

        sendBtn.addEventListener('click', sendMessage);
        input.addEventListener('keydown', function (event) {
            if (event.key === 'Enter') {
                event.preventDefault();
                sendMessage();
            }
        });
    }

    // Exposed so the render-event listener's mount seam can be re-invoked
    // (idempotency proof) without needing a second real event dispatch.
    window.oeCopilotMount = mountCopilotPanel;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mountCopilotPanel);
    } else {
        mountCopilotPanel();
    }

    // Node/Jest export seam only -- the browser never defines `module`, so
    // this is a no-op there and the IIFE's browser behavior above is
    // unaffected. Exposes the pure citation-chip parsing/building functions
    // for direct unit testing (T018).
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            KNOWN_RESOURCE_TYPES: KNOWN_RESOURCE_TYPES,
            humanizeResourceType: humanizeResourceType,
            shortenResourceId: shortenResourceId,
            parseReplySegments: parseReplySegments,
            buildAgentReplyFragment: buildAgentReplyFragment
        };
    }
})();
