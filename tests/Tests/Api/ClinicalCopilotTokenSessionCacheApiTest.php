<?php

/**
 * Clinical Co-Pilot session-cached FHIR token test (T040).
 *
 * T011 binds a conversation to its caller by hash_token(bearer). T027's
 * SmartLaunchTokenProvider mints a FRESH token on every getToken() call, so on a
 * follow-up turn the token hash differs and the conversation binding 404s. The fix:
 * cache the minted bearer in the relay's session, keyed by the open patient uuid, and
 * return the BYTE-IDENTICAL string within its TTL so hash_token() stays stable.
 *
 * Criteria asserted:
 *   C1 - with a session supplied, two getToken($patientUuid) calls for the same
 *        clinician+patient return the SAME (byte-identical) token string.
 *   C2 - a different patient uuid returns a DIFFERENT token (cache keyed by patient;
 *        no cross-patient reuse), and re-requesting the first patient still returns
 *        its own cached token (no collision/eviction).
 *   C3 - a cached token at/within the safety margin of expiry is NOT returned; a fresh,
 *        different token is minted and re-cached (seeded expired + seeded within-margin).
 *   C4 - with no session (null), each getToken() mints a fresh token (two calls -> two
 *        different tokens): the locked T027 no-session construction is unaffected.
 *   C5 - a guardrail-failing user still throws AgentUnavailableException and caches
 *        nothing, with or without a session (T027 C5 must not regress).
 *   C6 - the session is an OPTIONAL, nullable constructor argument defaulting to null,
 *        so the locked T027 construction (no session) stays backward compatible.
 *
 * These tests run against the live dev stack (real mint on every cache MISS).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Api;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Modules\ClinicalCopilot\AgentUnavailableException;
use OpenEMR\Modules\ClinicalCopilot\SmartLaunchTokenProvider;
use PHPUnit\Framework\TestCase;
use ReflectionMethod;
use ReflectionNamedType;
use Symfony\Component\HttpFoundation\Session\Session;
use Symfony\Component\HttpFoundation\Session\SessionInterface;
use Symfony\Component\HttpFoundation\Session\Storage\MockArraySessionStorage;

class ClinicalCopilotTokenSessionCacheApiTest extends TestCase
{
    /** Marker name the T027 launch/patient OAuth client is registered under. */
    private const SMART_CLIENT_NAME = 'oe-module-clinical-copilot smart-launch service';

    /** Session key the relay stores the per-patient token cache under (matches production). */
    private const SESSION_CACHE_KEY = 'copilot_fhir_token';

    /** Stable dev-seed clinician (id=5, role=users, passes patients/demo) — NOT admin. */
    private const CLINICIAN_USERNAME = 'clinician';
    /** Stable dev-seed user that fails the patients/demo ACL guardrail. */
    private const ACL_FAIL_USERNAME = 'phimail-service';
    /** Stable dev-seed user whose role is NOT `users` (fails the role guardrail). */
    private const ROLE_FAIL_USERNAME = 'oe-system';

    private int $clinicianId = 0;
    private string $clinicianUuid = '';
    private string $boundPatientUuid = '';
    private string $otherPatientUuid = '';

    protected function setUp(): void
    {
        $this->clinicianId = $this->requireUserId(self::CLINICIAN_USERNAME);
        $this->clinicianUuid = $this->uuidForUserId($this->clinicianId);

        $bound = QueryUtils::querySingleRow(
            'SELECT `pid`, `uuid` FROM `patient_data` WHERE `uuid` IS NOT NULL ORDER BY `pid` ASC LIMIT 1'
        );
        if (!is_array($bound) || !isset($bound['pid'], $bound['uuid'])) {
            $this->fail('Expected at least one patient with a uuid on the live stack.');
        }
        $this->boundPatientUuid = UuidRegistry::uuidToString($bound['uuid']);

        $other = QueryUtils::querySingleRow(
            'SELECT `uuid` FROM `patient_data` WHERE `uuid` IS NOT NULL AND `pid` <> ? ORDER BY `pid` ASC LIMIT 1',
            [(int) $bound['pid']]
        );
        if (!is_array($other) || !isset($other['uuid'])) {
            $this->fail('Expected a second patient with a uuid for the cross-patient probe.');
        }
        $this->otherPatientUuid = UuidRegistry::uuidToString($other['uuid']);
    }

    protected function tearDown(): void
    {
        // Scope-agnostic cleanup: delete every artifact keyed on the dedicated client.
        $clientRows = QueryUtils::fetchRecords(
            'SELECT `client_id` FROM `oauth_clients` WHERE `client_name` = ?',
            [self::SMART_CLIENT_NAME]
        );
        foreach ($clientRows as $row) {
            $clientId = is_array($row) ? ($row['client_id'] ?? null) : null;
            if (!is_string($clientId) || $clientId === '') {
                continue;
            }
            QueryUtils::sqlStatementThrowException('DELETE FROM `api_token` WHERE `client_id` = ?', [$clientId]);
            QueryUtils::sqlStatementThrowException(
                "DELETE FROM `oauth_trusted_user` WHERE `client_id` = ? AND `grant_type` = 'password_grant'",
                [$clientId]
            );
            QueryUtils::sqlStatementThrowException('DELETE FROM `oauth_clients` WHERE `client_id` = ?', [$clientId]);
        }
    }

    // ---------------------------------------------------------------- helpers

    private function requireUserId(string $username): int
    {
        $row = QueryUtils::querySingleRow('SELECT `id` FROM `users` WHERE `username` = ?', [$username]);
        if (!is_array($row) || !isset($row['id'])) {
            $this->fail("Expected dev-seed user '{$username}' to exist on the live stack.");
        }
        return (int) $row['id'];
    }

    private function uuidForUserId(int $id): string
    {
        $uuid = QueryUtils::fetchSingleValue('SELECT `uuid` FROM `users` WHERE `id` = ?', 'uuid', [$id]);
        if ($uuid === null || $uuid === '') {
            $this->fail("Expected user id={$id} to have a uuid.");
        }
        return UuidRegistry::uuidToString($uuid);
    }

    private function newSession(): Session
    {
        return new Session(new MockArraySessionStorage());
    }

    private function assertIsSignedJwt(string $token, string $why): void
    {
        $this->assertNotSame('', $token, $why);
        $this->assertCount(3, explode('.', $token), $why . ' (must be a 3-segment signed JWT)');
    }

    private function tokenCountForUser(string $userUuid): int
    {
        return count(QueryUtils::fetchRecords(
            'SELECT `id` FROM `api_token` WHERE `user_id` = ?',
            [$userUuid]
        ));
    }

    // ---------------------------------------------------------------------- C1

    public function testSameTokenReusedAcrossCallsForSameClinicianAndPatient(): void
    {
        $session = $this->newSession();
        $provider = new SmartLaunchTokenProvider($this->clinicianId, session: $session);

        $first = $provider->getToken($this->boundPatientUuid);
        $second = $provider->getToken($this->boundPatientUuid);

        $this->assertIsSignedJwt($first, 'First getToken must return a signed token.');
        // THE property that keeps hash_token() stable across turns: byte-identical string.
        $this->assertSame(
            $first,
            $second,
            'Two getToken() calls for the same clinician+patient must return the byte-identical token string.'
        );
    }

    // ---------------------------------------------------------------------- C2

    public function testDifferentPatientYieldsDifferentTokenWithoutCollision(): void
    {
        $session = $this->newSession();
        $provider = new SmartLaunchTokenProvider($this->clinicianId, session: $session);

        $boundToken = $provider->getToken($this->boundPatientUuid);
        $otherToken = $provider->getToken($this->otherPatientUuid);

        $this->assertIsSignedJwt($boundToken, 'Bound-patient token must be a signed token.');
        $this->assertIsSignedJwt($otherToken, 'Other-patient token must be a signed token.');
        $this->assertNotSame(
            $boundToken,
            $otherToken,
            'A different patient uuid must yield a different token (cache keyed by patient).'
        );

        // Re-requesting the first patient must still return ITS cached token, not the
        // second patient's — proving no cross-patient collision or eviction.
        $boundAgain = $provider->getToken($this->boundPatientUuid);
        $this->assertSame(
            $boundToken,
            $boundAgain,
            'The first patient must still resolve to its own cached token after a second patient was cached.'
        );
    }

    // ---------------------------------------------------------------------- C3

    public function testExpiredCachedEntryIsReMintedNotReturned(): void
    {
        $session = $this->newSession();
        // Seed an ALREADY-EXPIRED cache entry for the bound patient.
        $session->set(self::SESSION_CACHE_KEY, [
            $this->boundPatientUuid => ['token' => 'stale.expired.jwt', 'exp' => time() - 10],
        ]);

        $provider = new SmartLaunchTokenProvider($this->clinicianId, session: $session);
        $fresh = $provider->getToken($this->boundPatientUuid);

        $this->assertNotSame('stale.expired.jwt', $fresh, 'An expired cached token must not be returned.');
        $this->assertIsSignedJwt($fresh, 'A fresh, real token must be minted when the cache entry is expired.');

        // And the freshly minted token is now cached and reused on the next call.
        $again = $provider->getToken($this->boundPatientUuid);
        $this->assertSame($fresh, $again, 'The freshly minted token must be cached and reused.');
    }

    public function testCachedEntryWithinSafetyMarginIsReMinted(): void
    {
        $session = $this->newSession();
        // Not yet expired, but only 60s of life left — inside any sane >= 5-min safety margin.
        $session->set(self::SESSION_CACHE_KEY, [
            $this->boundPatientUuid => ['token' => 'about.to.expire', 'exp' => time() + 60],
        ]);

        $provider = new SmartLaunchTokenProvider($this->clinicianId, session: $session);
        $fresh = $provider->getToken($this->boundPatientUuid);

        $this->assertNotSame(
            'about.to.expire',
            $fresh,
            'A token within the expiry safety margin must be re-minted, not returned.'
        );
        $this->assertIsSignedJwt($fresh, 'A fresh, real token must be minted when within the safety margin.');
    }

    // ---------------------------------------------------------------------- C4

    public function testNoSessionMintsFreshTokenEveryCall(): void
    {
        // No session supplied -> unchanged T027 behavior: mint on every call.
        $provider = new SmartLaunchTokenProvider($this->clinicianId);

        $first = $provider->getToken($this->boundPatientUuid);
        $second = $provider->getToken($this->boundPatientUuid);

        $this->assertIsSignedJwt($first, 'First no-session getToken must return a signed token.');
        $this->assertIsSignedJwt($second, 'Second no-session getToken must return a signed token.');
        $this->assertNotSame(
            $first,
            $second,
            'With no session, each getToken() must mint a fresh (different) token.'
        );
    }

    // ---------------------------------------------------------------------- C5

    public function testGuardrailFailingUserFailsClosedAndCachesNothingWithSession(): void
    {
        // (a) ACL-failing user, WITH a session: must throw and cache nothing.
        $aclFailId = $this->requireUserId(self::ACL_FAIL_USERNAME);
        $aclFailUuid = $this->uuidForUserId($aclFailId);
        $beforeAcl = $this->tokenCountForUser($aclFailUuid);

        $session = $this->newSession();
        $aclProvider = new SmartLaunchTokenProvider($aclFailId, session: $session);
        try {
            $aclProvider->getToken($this->boundPatientUuid);
            $this->fail('A user failing the patients/demo ACL must not receive a token, even with a session.');
        } catch (AgentUnavailableException) {
            // expected — fails closed
        }
        $this->assertSame(
            $beforeAcl,
            $this->tokenCountForUser($aclFailUuid),
            'No token may be minted for an ACL-failing user.'
        );
        // Nothing was written into the session cache.
        $this->assertNull(
            $session->get(self::SESSION_CACHE_KEY),
            'A guardrail-failing user must cache nothing in the session.'
        );

        // (b) role-failing user, WITHOUT a session: still throws (T027 C5 unchanged).
        $roleFailId = $this->requireUserId(self::ROLE_FAIL_USERNAME);
        $roleFailUuid = $this->uuidForUserId($roleFailId);
        $beforeRole = $this->tokenCountForUser($roleFailUuid);

        $roleProvider = new SmartLaunchTokenProvider($roleFailId);
        try {
            $roleProvider->getToken($this->boundPatientUuid);
            $this->fail('A user whose role is not `users` must not receive a token.');
        } catch (AgentUnavailableException) {
            // expected — fails closed
        }
        $this->assertSame(
            $beforeRole,
            $this->tokenCountForUser($roleFailUuid),
            'No token may be minted for a non-users-role user.'
        );
    }

    // ---------------------------------------------------------------------- C6

    public function testConstructorAcceptsOptionalNullableSession(): void
    {
        $ctor = new ReflectionMethod(SmartLaunchTokenProvider::class, '__construct');

        $sessionParam = null;
        foreach ($ctor->getParameters() as $param) {
            $type = $param->getType();
            if ($type instanceof ReflectionNamedType && is_a($type->getName(), SessionInterface::class, true)) {
                $sessionParam = $param;
                break;
            }
        }

        $this->assertNotNull(
            $sessionParam,
            'The constructor must accept a Symfony SessionInterface parameter for the token cache.'
        );
        $this->assertTrue($sessionParam->isOptional(), 'The session parameter must be optional.');
        $this->assertTrue($sessionParam->allowsNull(), 'The session parameter must be nullable.');
        $this->assertNull(
            $sessionParam->getDefaultValue(),
            'The session parameter must default to null (backward-compatible with the locked T027 construction).'
        );

        // Backward compatibility: the locked T027 no-session construction still works.
        $this->assertInstanceOf(
            SmartLaunchTokenProvider::class,
            new SmartLaunchTokenProvider($this->clinicianId)
        );
    }
}
