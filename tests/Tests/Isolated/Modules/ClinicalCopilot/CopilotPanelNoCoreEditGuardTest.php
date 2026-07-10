<?php

/**
 * Structural guard for T019 criterion 2: no core file may be modified by the
 * Clinical Co-Pilot Dashboard panel scaffolding. The whole point of the
 * client-side injection design (RenderEvent listener + echoed on-disk assets)
 * is that it requires zero core edits.
 *
 * Diffs the working tree against the commit HEAD was pinned at when T019
 * began (f6ff2e2, the last commit before this ticket's work started),
 * excluding the module directory and tests/, and asserts the diff is empty.
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

class CopilotPanelNoCoreEditGuardTest extends TestCase
{
    /**
     * HEAD at the start of T019 ("docs(copilot): chart panel = client-side
     * right-column injection, no core edits (T019)"). Fixed and pre-dates
     * any T019 production or test code.
     */
    private const BASE_COMMIT = 'f6ff2e2fb5e2db2519e86f309dd0302d55791d0e';

    private const MODULE_DIRECTORY = 'interface/modules/custom_modules/oe-module-clinical-copilot';

    /**
     * Environment-generated site configuration, rewritten by the dev
     * container's entrypoint on every start with local DB credentials. Not
     * source code, not touched by T019, and expected to differ from the
     * pinned base commit purely as a side effect of the container running.
     */
    private const IGNORED_ENVIRONMENT_PATHS = ['sites'];

    public function testNoFileOutsideModuleOrTestsDiffersFromPreT019Base(): void
    {
        $repoRoot = $this->findRepoRoot();

        $excludePathspecs = array_map(
            static fn (string $path): string => escapeshellarg(':(exclude)' . $path),
            [self::MODULE_DIRECTORY, 'tests', ...self::IGNORED_ENVIRONMENT_PATHS]
        );

        $command = sprintf(
            'git -C %s diff --name-only %s -- . %s 2>&1',
            escapeshellarg($repoRoot),
            escapeshellarg(self::BASE_COMMIT),
            implode(' ', $excludePathspecs)
        );

        // exec(), not shell_exec(): shell_exec() returns NULL both on command
        // failure AND on a legitimate empty-output success (the expected
        // green case here, since git diff produces no output when there are
        // no differences). exec() gives an explicit exit code, so a real
        // failure (bad revision, git missing) is distinguishable from "ran
        // fine, nothing changed".
        exec($command, $outputLines, $exitCode);
        $this->assertSame(0, $exitCode, 'Expected `git diff` to run successfully (exit 0): ' . implode("\n", $outputLines));

        $changedPaths = array_values(array_filter(array_map('trim', $outputLines)));

        $this->assertSame(
            [],
            $changedPaths,
            'Files changed outside the module directory and tests/ (core-edit guard tripped): '
                . implode(', ', $changedPaths)
        );
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
