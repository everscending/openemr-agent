<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Eric Stern <erics@opencoreemr.com>
 * @copyright Copyright (c) 2026 OpenCoreEMR Inc <https://opencoreemr.com/>
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\PHPUnit;

use PHPUnit\Runner\Extension\Extension as PHPUnitExtension;
use PHPUnit\Runner\Extension\Facade;
use PHPUnit\Runner\Extension\ParameterCollection;
use PHPUnit\TextUI\Configuration\Configuration;

/**
 * https://docs.phpunit.de/en/12.5/extending-phpunit.html#implementing-an-extension
 */
class Extension implements PHPUnitExtension
{
    /**
     * Tracks if bootstrapping occurred. PHPUnit itself instantiates this class
     * so we can't track the setup ourselves.
     */
    private static bool $isBootstrapped = false;

    public function bootstrap(
        Configuration $configuration,
        Facade $facade,
        ParameterCollection $parameters
    ): void {
        $shutdownTracker = new ShutdownTracker();
        $shutdownTracker->install();
        $facade->registerSubscriber($shutdownTracker);

        self::registerCustomModuleAutoloaders();

        self::$isBootstrapped = true;
    }

    /**
     * Custom modules under interface/modules/custom_modules are loaded at runtime
     * by ModulesClassLoader for the web app, but the composer autoloader used by
     * the (bootstrap-less) isolated test suite has no knowledge of their PSR-4
     * namespaces. Register each module's declared PSR-4 mapping here — during the
     * extension bootstrap, which runs before test files are discovered/loaded —
     * so isolated tests can exercise module classes directly. Purely additive:
     * the registered autoloader only handles prefixes a module declares and is a
     * no-op for everything else.
     */
    private static function registerCustomModuleAutoloaders(): void
    {
        $customModulesDir = dirname(__DIR__, 2) . '/interface/modules/custom_modules';
        $manifests = glob($customModulesDir . '/*/composer.json') ?: [];

        /** @var array<string, string> $prefixes namespace prefix => src directory */
        $prefixes = [];
        foreach ($manifests as $manifest) {
            $decoded = json_decode((string) file_get_contents($manifest), true);
            if (!is_array($decoded)) {
                continue;
            }
            $psr4 = $decoded['autoload']['psr-4'] ?? null;
            if (!is_array($psr4)) {
                continue;
            }
            foreach ($psr4 as $namespace => $relativeDir) {
                if (!is_string($namespace) || !is_string($relativeDir)) {
                    continue;
                }
                $prefixes[$namespace] = rtrim(dirname($manifest) . '/' . $relativeDir, '/') . '/';
            }
        }

        if ($prefixes === []) {
            return;
        }

        spl_autoload_register(static function (string $class) use ($prefixes): void {
            foreach ($prefixes as $namespace => $baseDir) {
                if (!str_starts_with($class, $namespace)) {
                    continue;
                }
                $relative = substr($class, strlen($namespace));
                $file = $baseDir . str_replace('\\', '/', $relative) . '.php';
                if (is_file($file)) {
                    require_once $file;
                    return;
                }
            }
        });
    }

    public static function isBootstrapped(): bool
    {
        return self::$isBootstrapped;
    }
}
