<?php

/**
 * Service-account bearer provider for the co-pilot relay (T021).
 *
 * TOKEN DECISION (made by the user; SMART deferred). The demo does NOT mint a
 * per-user SMART token. It uses one OAuth2 service-account bearer for the
 * existing `admin` OpenEMR user, obtained server-side via the password grant and
 * forwarded to the agent. See "Attribution limitation" in the T021 ticket: the
 * T016 audit rows will name the service user, not the clinician. The real fix is
 * the deferred SMART EHR-launch ticket.
 *
 * Client provisioning is idempotent: a single enabled OAuth2 client (named by
 * CLIENT_NAME) is registered ONCE and reused. Because the password/refresh
 * grant type does not check `client_secret` at all (verified against the live
 * dev stack — ClientRepository::validateClient returns true for these grants),
 * the relay holds no client secret; the real gate is the client's `is_enabled`
 * flag, which registration sets to 0 and which this provisioner flips to 1 via
 * ClientRepository::saveIsEnabled — the same step the API test harness makes.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use OpenEMR\Common\Auth\OpenIDConnect\Entities\ClientEntity;
use OpenEMR\Common\Auth\OpenIDConnect\Repositories\ClientRepository;
use OpenEMR\Common\Database\QueryUtils;
use Psr\Log\NullLogger;
use RuntimeException;

final class OAuthServiceTokenProvider implements ServiceTokenProvider
{
    /** Marker name the single provisioned relay OAuth2 client is registered under. */
    public const CLIENT_NAME = 'oe-module-clinical-copilot relay service';

    /**
     * Concrete resource scopes the agent's FHIR tools need. Meta-scopes
     * (api:fhir/api:oemr) are intentionally omitted: they do not persist into the
     * granted scope of a password-grant token.
     */
    private const SCOPES = 'openid offline_access '
        . 'patient/Patient.read user/Patient.read patient/Observation.read '
        . 'patient/AllergyIntolerance.read patient/MedicationRequest.read patient/Condition.read';

    public function __construct(
        private readonly string $oauthBaseUrl = 'https://localhost',
        private readonly string $serviceUsername = 'admin',
        private readonly string $servicePassword = 'pass',
        private readonly int $timeoutSeconds = 10,
    ) {
    }

    public function getToken(): string
    {
        // TODO(SMART): per-user token, see deferred ticket
        $clientId = $this->ensureProvisionedClientId();
        return $this->passwordGrant($clientId);
    }

    private function ensureProvisionedClientId(): string
    {
        $existing = $this->lookupEnabledClientId();
        if ($existing !== null) {
            return $existing;
        }
        $clientId = $this->register();
        $this->enable($clientId);
        return $clientId;
    }

    private function lookupEnabledClientId(): ?string
    {
        $rows = QueryUtils::fetchRecords(
            'SELECT `client_id` FROM `oauth_clients` WHERE `client_name` = ? AND `is_enabled` = 1 '
            . 'ORDER BY `register_date` DESC LIMIT 1',
            [self::CLIENT_NAME]
        );
        $clientId = $rows[0]['client_id'] ?? null;
        return is_string($clientId) && $clientId !== '' ? $clientId : null;
    }

    private function register(): string
    {
        $response = $this->httpJson(
            '/oauth2/default/registration',
            [
                'application_type' => 'private',
                'redirect_uris' => ['https://localhost/callback'],
                'client_name' => self::CLIENT_NAME,
                'token_endpoint_auth_method' => 'client_secret_post',
                'contacts' => ['clinical-copilot@example.org'],
                'scope' => self::SCOPES,
                // MUST be explicit: absent, the server defaults to
                // authorization_code and the password grant is rejected.
                'grant_types' => ['password', 'refresh_token'],
            ]
        );
        $clientId = $response['client_id'] ?? null;
        if (!is_string($clientId) || $clientId === '') {
            throw new AgentUnavailableException('service client registration returned no client_id');
        }
        return $clientId;
    }

    private function enable(string $clientId): void
    {
        try {
            $repository = new ClientRepository();
            $repository->setSystemLogger(new NullLogger());
            $entity = $repository->getClientEntity($clientId);
            if (!$entity instanceof ClientEntity) {
                throw new AgentUnavailableException('service client could not be loaded for enablement');
            }
            $repository->saveIsEnabled($entity, true);
        } catch (RuntimeException $e) {
            // saveIsEnabled throws RuntimeException on sql failure; do not leak it.
            throw new AgentUnavailableException('service client could not be enabled', 0, $e);
        }
    }

    private function passwordGrant(string $clientId): string
    {
        $response = $this->httpForm(
            '/oauth2/default/token',
            [
                'grant_type' => 'password',
                'client_id' => $clientId,
                'scope' => self::SCOPES,
                'user_role' => 'users',
                'username' => $this->serviceUsername,
                'password' => $this->servicePassword,
            ]
        );
        $token = $response['access_token'] ?? null;
        if (!is_string($token) || $token === '') {
            throw new AgentUnavailableException('service token grant returned no access_token');
        }
        return $token;
    }

    /**
     * @param array<string, mixed> $body
     * @return array<string, mixed>
     */
    private function httpJson(string $path, array $body): array
    {
        $encoded = json_encode($body);
        if ($encoded === false) {
            throw new AgentUnavailableException('failed to encode oauth request');
        }
        return $this->execute($path, $encoded, 'Content-Type: application/json');
    }

    /**
     * @param array<string, string> $body
     * @return array<string, mixed>
     */
    private function httpForm(string $path, array $body): array
    {
        return $this->execute($path, http_build_query($body), 'Content-Type: application/x-www-form-urlencoded');
    }

    /**
     * @return array<string, mixed>
     */
    private function execute(string $path, string $body, string $contentTypeHeader): array
    {
        $handle = curl_init(rtrim($this->oauthBaseUrl, '/') . $path);
        if ($handle === false) {
            throw new AgentUnavailableException('failed to initialize oauth transport');
        }
        curl_setopt_array($handle, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_HTTPHEADER => [$contentTypeHeader, 'Accept: application/json'],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_SSL_VERIFYPEER => false,
            CURLOPT_SSL_VERIFYHOST => false,
            CURLOPT_CONNECTTIMEOUT => $this->timeoutSeconds,
            CURLOPT_TIMEOUT => $this->timeoutSeconds,
        ]);
        $raw = curl_exec($handle);
        $errno = curl_errno($handle);
        $status = (int) curl_getinfo($handle, CURLINFO_HTTP_CODE);

        if ($errno !== 0 || !is_string($raw)) {
            throw new AgentUnavailableException('oauth transport error (' . $errno . ')');
        }
        if ($status < 200 || $status >= 300) {
            throw new AgentUnavailableException('oauth endpoint returned status ' . $status);
        }
        $decoded = json_decode($raw, true);
        if (!is_array($decoded)) {
            throw new AgentUnavailableException('oauth endpoint returned an unparseable response');
        }
        /** @var array<string, mixed> $decoded */
        return $decoded;
    }
}
