<?php

/**
 * Wire-record parser for the audit-bridge endpoint (T016).
 *
 * Validates the ENTIRE record before any row is written — a malformed record
 * never reaches a DB write (criterion 5). Returns null on any rejection; the
 * controller renders one byte-identical 422 for all of them so the caller can
 * not distinguish "malformed" from "unknown patient" (no existence oracle).
 *
 * Tolerant-in / strict-out: the 9 non-nullable keys must be present; degraded
 * and fallback_reason may be absent or JSON null. Any unknown key is rejected
 * (mirrors the sender's extra="forbid").
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    OpenEMR Clinical Co-Pilot <support@open-emr.org>
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use DateMalformedStringException;
use DateTimeImmutable;

final class AuditRecordParser
{
    private const ALLOWED_KEYS = [
        'user_token_hash',
        'patient_id',
        'correlation_id',
        'conversation_id',
        'occurred_at',
        'claims_total',
        'claims_passed',
        'claims_stripped',
        'outcome',
        'degraded',
        'fallback_reason',
    ];

    private const REQUIRED_KEYS = [
        'user_token_hash',
        'patient_id',
        'correlation_id',
        'conversation_id',
        'occurred_at',
        'claims_total',
        'claims_passed',
        'claims_stripped',
        'outcome',
    ];

    private const NON_EMPTY_STRING_KEYS = [
        'user_token_hash',
        'patient_id',
        'correlation_id',
        'conversation_id',
        'occurred_at',
        'outcome',
    ];

    private const COUNT_KEYS = ['claims_total', 'claims_passed', 'claims_stripped'];

    private const OUTCOMES = ['answered', 'fallback', 'degraded'];

    private const DEGRADED_REASONS = ['llm_unavailable'];

    private const FALLBACK_REASONS = [
        'refusal',
        'max_tokens',
        'malformed_output',
        'tool_args_invalid',
        'step_cap_exceeded',
    ];

    public static function parse(string $json): ?AuditRecord
    {
        $decoded = json_decode($json, true);
        if (!is_array($decoded)) {
            return null;
        }

        // Reject any unknown/extra key (not only the ones we happen to name).
        foreach (array_keys($decoded) as $key) {
            if (!in_array($key, self::ALLOWED_KEYS, true)) {
                return null;
            }
        }

        foreach (self::REQUIRED_KEYS as $key) {
            if (!array_key_exists($key, $decoded)) {
                return null;
            }
        }

        foreach (self::NON_EMPTY_STRING_KEYS as $key) {
            if (!is_string($decoded[$key]) || $decoded[$key] === '') {
                return null;
            }
        }

        foreach (self::COUNT_KEYS as $key) {
            // is_int rejects floats, numeric strings, and booleans.
            if (!is_int($decoded[$key]) || $decoded[$key] < 0) {
                return null;
            }
        }

        $userTokenHash = $decoded['user_token_hash'];
        if (preg_match('/\A[0-9a-f]{64}\z/', $userTokenHash) !== 1) {
            return null;
        }

        $outcome = $decoded['outcome'];
        if (!in_array($outcome, self::OUTCOMES, true)) {
            return null;
        }

        $degraded = array_key_exists('degraded', $decoded) ? $decoded['degraded'] : null;
        if ($degraded !== null) {
            if (!is_string($degraded) || !in_array($degraded, self::DEGRADED_REASONS, true)) {
                return null;
            }
        }

        $fallbackReason = array_key_exists('fallback_reason', $decoded) ? $decoded['fallback_reason'] : null;
        if ($fallbackReason !== null) {
            if (!is_string($fallbackReason) || !in_array($fallbackReason, self::FALLBACK_REASONS, true)) {
                return null;
            }
        }

        // Cross-field consistency, keyed on outcome.
        if ($outcome === 'degraded') {
            if ($degraded === null || $fallbackReason !== null) {
                return null;
            }
        } elseif ($outcome === 'fallback') {
            if ($fallbackReason === null || $degraded !== null) {
                return null;
            }
        } else { // answered
            if ($degraded !== null || $fallbackReason !== null) {
                return null;
            }
        }

        $claimsTotal = $decoded['claims_total'];
        $claimsPassed = $decoded['claims_passed'];
        $claimsStripped = $decoded['claims_stripped'];
        // A claim may be neither passed nor stripped, so do not demand equality;
        // only reject the impossible over-count.
        if (($claimsPassed + $claimsStripped) > $claimsTotal) {
            return null;
        }

        $occurredAt = self::parseRfc3339($decoded['occurred_at']);
        if ($occurredAt === null) {
            return null;
        }

        return new AuditRecord(
            $userTokenHash,
            $decoded['patient_id'],
            $decoded['correlation_id'],
            $decoded['conversation_id'],
            $occurredAt,
            $claimsTotal,
            $claimsPassed,
            $claimsStripped,
            $outcome,
            $degraded,
            $fallbackReason,
        );
    }

    /**
     * RFC 3339 with an explicit offset (Z or numeric). A naive, offset-less
     * timestamp is invalid.
     */
    private static function parseRfc3339(string $value): ?DateTimeImmutable
    {
        $pattern = '/\A\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+\-]\d{2}:\d{2})\z/';
        if (preg_match($pattern, $value) !== 1) {
            return null;
        }
        try {
            return new DateTimeImmutable($value);
        } catch (DateMalformedStringException) {
            // PHP 8.3+ throws this for a value the constructor cannot parse.
            // Narrowed from Throwable so a programming \Error is never swallowed.
            return null;
        }
    }
}
