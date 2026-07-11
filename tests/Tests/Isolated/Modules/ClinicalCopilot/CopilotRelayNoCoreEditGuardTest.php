<?php

/**
 * Structural guard for T021 criterion 5: the panel -> agent relay is entirely
 * module-side. No file outside the module directory (and tests/) may be
 * modified — the relay endpoint, its client-provisioning step and the panel JS
 * extension all live under the module, and no CORS/core change is made to
 * OpenEMR.
 *
 * Diffs the working tree against the commit HEAD was pinned at when T021 began
 * (ea778ff, the last commit before this ticket's work started), excluding the
 * module directory and tests/, and asserts the diff is empty. The guard itself
 * is proven to be able to FAIL by the sibling test below (a planted path that
 * would be reported were it outside the module).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot TDD run
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Modules\ClinicalCopilot;

use PHPUnit\Framework\TestCase;

final class CopilotRelayNoCoreEditGuardTest extends TestCase
{
    /**
     * HEAD at the start of T021 ("feat(copilot): T020 agent service container +
     * dev compose wiring"). Fixed and pre-dates any T021 production or test code.
     */
    private const BASE_COMMIT = 'ea778ff4b414bb0b9b367801555e833dc66bf721';

    private const MODULE_DIRECTORY = 'interface/modules/custom_modules/oe-module-clinical-copilot';

    /**
     * Environment-generated site configuration, rewritten by the dev
     * container's entrypoint on every start with local DB credentials. Not
     * source code, not touched by T021.
     */
    private const IGNORED_ENVIRONMENT_PATHS = ['sites'];

    public function testNoFileOutsideModuleOrTestsDiffersFromPreT021Base(): void
    {
        $repoRoot = $this->findRepoRoot();

        $changedPaths = $this->changedPathsSinceBase($repoRoot, [
            self::MODULE_DIRECTORY,
            'tests',
            ...self::IGNORED_ENVIRONMENT_PATHS,
        ]);

        $this->assertSame(
            [],
            $changedPaths,
            'Files changed outside the module directory and tests/ (core-edit guard tripped): '
                . implode(', ', $changedPaths)
        );
    }

    /**
     * Prove the guard can fail: without the module-directory exclusion, the
     * relay's own new files under the module MUST show up as changed. A guard
     * that can never report anything is not a guard.
     */
    public function testGuardActuallyDetectsModuleFilesWhenNotExcluded(): void
    {
        $repoRoot = $this->findRepoRoot();

        // Exclude only tests/ and env paths, NOT the module directory.
        $changedPaths = $this->changedPathsSinceBase($repoRoot, [
            'tests',
            ...self::IGNORED_ENVIRONMENT_PATHS,
        ]);

        $relayFiles = array_filter(
            $changedPaths,
            static fn (string $p): bool => str_starts_with($p, self::MODULE_DIRECTORY)
        );

        $this->assertNotEmpty(
            $relayFiles,
            'Expected the relay implementation to introduce/modify files under the module directory; '
                . 'if this is empty the diff machinery itself is broken and the sibling guard is vacuous.'
        );
    }

    /**
     * @param list<string> $excludePaths
     * @return list<string>
     */
    private function changedPathsSinceBase(string $repoRoot, array $excludePaths): array
    {
        $excludePathspecs = array_map(
            static fn (string $path): string => escapeshellarg(':(exclude)' . $path),
            $excludePaths
        );

        $command = sprintf(
            'git -C %s diff --name-only %s -- . %s 2>&1',
            escapeshellarg($repoRoot),
            escapeshellarg(self::BASE_COMMIT),
            implode(' ', $excludePathspecs)
        );

        exec($command, $outputLines, $exitCode);
        $this->assertSame(0, $exitCode, 'Expected `git diff` to run successfully (exit 0): ' . implode("\n", $outputLines));

        return array_values(array_filter(array_map('trim', $outputLines)));
    }

    private function findRepoRoot(): string
    {
        $dir = __DIR__;
        while ($dir !== '/' && !is_dir($dir . '/.git')) {
            $parent = dirname($dir);
            if ($parent === $dir) {
                break;
            }
            $dir = $parent;
        }
        if (!is_dir($dir . '/.git')) {
            throw new \RuntimeException('Could not locate repository root starting from ' . __DIR__);
        }
        return $dir;
    }
}
