from __future__ import annotations

from .env import _envFlag, _envText

# == Core Bot ==
# Jane's Discord bot token. Keep this in `.env`, not in versioned config.
token = _envText("DISCORD_BOT_TOKEN")

# Primary servers.
serverId = 0
serverIdTesting = 0
testGuildIds = []


# == Credentials / External APIs ==
# Keep API keys and credential paths in `.env`, not in versioned config.
# Roblox, RoVer, and Sheets credentials.
robloxOpenCloudApiKey = _envText("ROBLOX_OPEN_CLOUD_API_KEY")
roverApiKey = _envText("ROVER_API_KEY")
orbatGoogleCredentialsPath = _envText("ORBAT_GOOGLE_CREDENTIALS_PATH")
googleOauthClientSecretsPath = _envText("GOOGLE_OAUTH_CLIENT_SECRETS_PATH")
googleOauthTokenPath = _envText("GOOGLE_OAUTH_TOKEN_PATH", "localOnly/credentials/google-oauth-token.json")

# Optional dedicated inventory key.
robloxInventoryApiKey = _envText("ROBLOX_INVENTORY_API_KEY", robloxOpenCloudApiKey)

# Feature-specific external service credentials.
bgIntelligenceTaseApiToken = _envText("TASE_API_TOKEN")
bgIntelligenceMocoApiKey = _envText("MOCO_API_KEY")
gamblingApiToken = _envText("JANE_GAMBLING_API_TOKEN")
freedcampApiKey = _envText("FREEDCAMP_API_KEY")
freedcampSecret = _envText("FREEDCAMP_SECRET")

# == Command Access / Runtime ==
# Allowed servers for command usage.
allowedCommandGuildIds = []

# Reserved runtime override users.
overridingUserIds = []
# Command sync toggles.
clearGlobalCommands = False
clearGuildCommands = False

# Temporary command lock.
temporaryCommandLockEnabled = False
temporaryCommandAllowedUserIds = []

# Runtime / diagnostics access.
errorMirrorUserId = 0
janeTerminalAllowedUserId = errorMirrorUserId
opsAllowedUserIds = []
runtimeControlAllowedUserIds = []
permissionSimulatorGuildIds = []

# Runtime task tuning.
sessionMessageUpdateDebounceSec = 2.0
runtimeBudgetRobloxConcurrency = 6
runtimeBudgetLowPriorityRobloxPriority = 50
runtimeBudgetLowestPriorityRobloxPriority = 1000
runtimeBudgetInteractiveDiscordPriority = -100
runtimeBudgetLowPriorityDiscordPriority = 50
runtimeBudgetLowestPriorityDiscordPriority = 1000
runtimeBudgetSheetsConcurrency = 2
runtimeBudgetInteractiveSheetsConcurrency = 2
runtimeBudgetBackgroundSheetsConcurrency = 1
runtimeBudgetDiscordConcurrency = 6
runtimeBudgetInteractionAckConcurrency = 24
runtimeBudgetBackgroundConcurrency = 2
runtimeTaskStatsPath = "runtime/data/task-stats.json"
eventLoopWatchdogEnabled = True
eventLoopWatchdogIntervalSec = 5.0
eventLoopWatchdogWarnAfterSec = 2.0
eventLoopWatchdogStackTaskLimit = 8

# Runtime database snapshots stay local/ignored. They are intentionally not
# committed to git, but they give us a recoverable DB copy around restarts.
dbRuntimeSnapshotEnabled = True
dbRuntimeSnapshotOnStartup = True
dbRuntimeSnapshotOnShutdown = True
dbRuntimeSnapshotDir = "backups/dbSnapshots"
dbRuntimeSnapshotRetention = 20
dbRuntimeDiagnosticReportPath = "runtime/data/db-state/latest.json"
runtimeTaskStatsFlushIntervalSec = 30
runtimeTaskStatsFlushDirtyCount = 25
discordEntityCacheTtlSec = 300
bgQueueUpdateConcurrency = 2
sessionMessageUpdateConcurrency = 2
bgQueueRepostConcurrency = 1
retryQueuePollIntervalSec = 6
retryQueueInitialDelaySec = 30
webhookHealthCheckIntervalSec = 600
webhookHealthInitialDelaySec = 180
webhookHealthMaxRowsPerRun = 50
roleOrbatSyncBackgroundDelaySec = 5
reminderDueBatchLimit = 20
reminderDeliveryConcurrency = 3
generalErrorLogDir = ""
generalErrorLogMaxBytes = 2 * 1024 * 1024
generalErrorLogBackupCount = 5
automationReportChannelId = 0
autoGitUpdateEnabled = False
enablePrivateExtensions = False
enableDestructiveCommands = False
destructiveCommandsDryRun = True
disableGitPullOnManualRestart = True
allowGitPullOnManualRestart = False
autoGitUpdateRemote = "origin"
autoGitUpdateBranch = ""
autoGitUpdateCheckIntervalSec = 60
autoGitUpdateInitialDelaySec = 120
autoGitUpdatePauseDrainSec = 5
autoGitUpdateInstallRequirements = _envFlag("JANE_INSTALL_REQUIREMENTS_ON_UPDATE", False)
autoGitUpdateDependencyInstallTimeoutSec = 600
# Timeout for individual git fetch/pull/stash/status commands.
autoGitUpdateGitCommandTimeoutSec = 120
# Extra paths to preserve in addition to the updater's built-in runtime defaults.
autoGitUpdatePreservePaths = [
    "backups/serverSnapshots",
    "backups/serverSnapshotsOffsite",
]
copyServerRoleBatchCreateLimit = 12
copyServerRoleBatchMutationLimit = 18

# Optional extension layers.
extraExtensionNames: list[str] = []
destructiveCommandGuildIds = []
destructiveCommandCooldownSec = 30

# Optional config sanity suppressions (ID keys intentionally left unset).
configSanityOptionalIdKeys = [
    "bestOfFormerMrRoleId",
    "bestOfFormerHrRoleId",
    "bestOfFormerAnrocomRoleId",
    "bestOfAnrocomRoleId",
    "projectHodRoleIds",
    "projectAssistantDirectorRoleIds",
]


# == Shared Role IDs ==
# Core moderation / training roles.
moderatorRoleId = 0  # BG Check Certified
bgReviewModeratorRoleId = 0  # BG reviewers in the review server
instructorRoleId = 0  # Training and Qualifications
newApplicantRoleId = 0  # New Applicant
pendingBgRoleId = 0  # Pending Background Check

# Shared rank / clearance roles.
middleRankRoleId = 0
highRankRoleId = 0
cnoRoleId = 0
dooRoleId = 0
ddooRoleId = 0
sectionChiefRoleId = 0
commandStaffRoleId = 0
foiRoleId = 0
crsRoleId = 0
shiftSupervisorRoleId = 0
juniorSuRoleId = 0
msbRoleId = 0

# Recruitment / ANRORS roles.
recruiterRoleId = 0  # CE Recruitment submitter role
recruitmentReviewerRoleId = 0
recruitmentReviewerPingRoleId = 0
anrorsMemberRoleId = 0  # ANRO Recruitment Services
anrorsRmPlusRoleId = 0  # ANRORS RM+

# Honor Guard roles.
honorGuardReviewerRoleId = 0
honorGuardReviewerPingRoleId = 0
honorGuardSeniorGuardsmanRoleId = 0
honorGuardPlatoonSergeantRoleId = 0
honorGuardParadeOfficerPlusRoleIds = []
honorGuardRoleId = 0

# ANRD role placeholders (for future role -> ORBAT rank sync).
anrdRoleProbationaryId = 0
anrdRoleContributorId = 0
anrdRoleSeniorContributorId = 0
anrdRoleDeveloperId = 0
anrdRoleSeniorDeveloperId = 0
anrdRoleDevelopmentProjectLeadId = 0
# Only ORBAT ranks (no Discord role mapping):
# - Development Oversight
# - Development Creator and Director
anrdRoleDevelopmentOversightId = 0
anrdRoleDevelopmentCreatorAndDirectorId = 0
anrdFundingBenefactorsRoleId = 0
