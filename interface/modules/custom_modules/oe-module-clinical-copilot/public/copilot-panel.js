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
 */
(function () {
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

        function appendAgentBubble(reply) {
            var bubble = document.createElement('div');
            bubble.className = 'oe-copilot__bubble oe-copilot__bubble--agent';
            // textContent only -- an LLM reply must never be parsed as markup.
            bubble.textContent = reply;
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
})();
