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
 * seam: it is static-shell only for T019 (no agent call, no network) and
 * appends user text via textContent only, never innerHTML, so there is no
 * injection surface.
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
                // Adversarial: empty/whitespace-only input appends nothing.
                return;
            }
            var bubble = document.createElement('div');
            bubble.className = 'oe-copilot__bubble oe-copilot__bubble--user';
            // textContent only -- never innerHTML -- no injection surface.
            bubble.textContent = text;
            list.appendChild(bubble);
            list.scrollTop = list.scrollHeight;
            input.value = '';
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
