<?php

/**
 * Clinical Co-Pilot Module Information
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    OpenEMR Clinical Co-Pilot <support@open-emr.org>
 * @copyright Copyright (c) 2026 OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

return [
    'name' => 'Clinical Co-Pilot',
    'description' => 'Internal audit-bridge endpoint for the Clinical Co-Pilot agent service. '
        . 'Records AI-mediated clinical summarization invocations to the OpenEMR decision log and '
        . 'HIPAA §164.528 disclosure accounting.',
    'version' => '1.0.0',
    'author' => 'OpenEMR Clinical Co-Pilot',
    'email' => 'support@open-emr.org',
    'license' => 'GPL-3.0',
    'acl_category' => 'admin',
    'acl_section' => 'users',

    // Module dependencies
    'require' => [
        'openemr' => '>=7.0.0',
    ],

    // This module owns no tables of its own; it writes existing audit tables.
    'tables' => [],
];
