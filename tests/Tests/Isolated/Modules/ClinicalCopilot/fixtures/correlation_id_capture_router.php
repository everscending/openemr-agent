<?php

/**
 * Router for a `php -S` test double standing in for the agent's /chat
 * endpoint (T046). Appends the received X-Correlation-ID header value (or the
 * literal string "MISSING" when absent) as one line to the log file named by
 * the CORRELATION_LOG_FILE environment variable, then answers with a
 * minimally valid T011 /chat JSON body so HttpCopilotAgentChatClient::chat()
 * decodes it successfully.
 *
 * Started via `php -S 127.0.0.1:<port> correlation_id_capture_router.php`
 * from CorrelationIdBoundaryTest.php — never invoked directly.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

$logFile = getenv('CORRELATION_LOG_FILE');
if ($logFile !== false) {
    $headers = function_exists('getallheaders') ? getallheaders() : [];
    $received = 'MISSING';
    foreach ($headers as $name => $value) {
        if (strcasecmp($name, 'X-Correlation-ID') === 0) {
            $received = $value;
            break;
        }
    }
    file_put_contents($logFile, $received . "\n", FILE_APPEND | LOCK_EX);
}

header('Content-Type: application/json');
echo json_encode([
    'reply' => 'ok',
    'conversation_id' => 'conv-router-fixed',
    'fallback' => false,
]);
