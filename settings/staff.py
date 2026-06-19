from __future__ import annotations

from .core import *
from .env import _envText

# == Session / BG Check ==
bgCheckChannelId = 0
bgCheckAdultReviewGuildId = 0
bgCheckAdultReviewChannelId = 0
bgCheckMinorReviewGuildId = 0
bgCheckMinorReviewChannelId = 0
bgCheckMinorReviewRoleId = 0
bgCheckMinorReviewRoleIds = []
orientationSpreadsheetForumTagIds = {
    1491904301184974940: 1491904838823579648,
    1491920876848025840: 1491922830751830180,
}
orientationRoverWarmupEnabled = True
orientationRoverWarmupDelaySec = 600
orientationRoverWarmupIntervalSec = 60
bgMinorAgeRoleIds = []
bgMajorAgeRoleIds = []
bgMinorAgeGroups = ["13-15", "16-17", "NO INFO"]
bgAdultAgeGroups = ["18-20", "21+"]
bgUnknownDefaultsToMinor = True
# Source guild for ?bgcheck member collection (defaults to serverId when unset).
bgCheckSourceGuildId = serverId
bgCheckSpreadsheetTemplateId = _envText(
    "BGC_SPREADSHEET_TEMPLATE_ID",
    "",
)
bgCheckSpreadsheetFolderId = _envText(
    "BGC_SPREADSHEET_FOLDER_ID",
    "",
)
bgCheckSpreadsheetSheetName = _envText("BGC_SPREADSHEET_SHEET_NAME", "Sheet1")
bgIntelligenceReportChannelId = 0
bgIntelligenceSheetLookupLimit = 12
bgItemReviewQueueEnabled = True
bgItemReviewQueueChannelId = 0
bgItemReviewReviewerRoleId = bgReviewModeratorRoleId
bgItemReviewWebhookName = "Jane BG Item Review"
bgItemReviewMaxPagesPerType = 4
bgItemReviewCandidateLimit = 60
bgFlagOpenGuildIds = []
bgItemReviewSpreadsheetSyncEnabled = False
bgItemReviewSpreadsheetSyncIntervalSec = 300
bgItemReviewSpreadsheetStartupLookbackDays = 5
bgItemReviewSpreadsheetRecurringLookbackDays = 1
bgItemReviewSpreadsheetSyncScanLimit = 10
bgItemReviewSpreadsheetSyncMaxRows = 250
bgRiskScoreBase = 20
bgRiskScoreFloor = 5
bgIntelligenceFetchGroupsEnabled = True
bgIntelligenceFetchConnectionsEnabled = True
bgIntelligenceFetchUsernameHistoryEnabled = True
bgIntelligenceFetchInventoryEnabled = True
bgIntelligenceFetchGamepassesEnabled = True
bgIntelligenceFetchBadgesEnabled = True
bgIntelligenceFetchFavoriteGamesEnabled = True
bgIntelligenceFetchOutfitsEnabled = True
bgIntelligenceFetchBadgeHistoryEnabled = True
bgIntelligenceKnownMemberAltDetectionEnabled = True
bgIntelligenceFetchFriendIdsEnabled = True
bgIntelligenceExternalSourcesEnabled = True
bgIntelligenceTaseEnabled = True
bgIntelligenceTaseApiBaseUrl = "https://api.tasebot.org"
bgIntelligenceTaseTimeoutSec = 10
bgIntelligenceMocoEnabled = True
bgIntelligenceMocoApiBaseUrl = "https://api.moco-co.org"
bgIntelligenceMocoTimeoutSec = 10
bgIntelligenceFavoriteGameMax = 25
bgIntelligenceOutfitMax = 25
bgIntelligenceUsernameHistoryMax = 50
bgIntelligenceKnownMemberAltMatchLimit = 10
bgIntelligenceKnownMemberAltCandidateLimit = 500
bgIntelligenceKnownMemberAltFuzzyEnabled = False
bgIntelligenceKnownMemberAltFuzzyMinSimilarity = 0.9
bgIntelligenceKnownMemberAltFuzzyMinLength = 5
bgIntelligenceKnownMemberAltGroupOverlapMin = 2
bgIntelligenceKnownMemberAltGroupOverlapMaxMemberCount = 50000
bgIntelligenceKnownMemberAltFriendLimit = 200
bgIntelligenceKnownMemberAltWords = [
    "alt",
    "alts",
    "backup",
    "backups",
    "back_up",
    "bak",
    "bckup",
    "spare",
    "second",
    "secondaccount",
    "account",
    "acct",
    "acc",
    "clone",
    "copy",
    "new",
    "old",
    "main",
    "again",
]
bgIntelligenceInventoryMaxPages = 0
bgIntelligenceInventoryHardMaxPages = 100
bgIntelligencePublicInventoryMaxPagesPerType = 10
bgIntelligenceInventoryFuzzyMatchingEnabled = True
bgIntelligenceInventoryFuzzyScoreCutoff = 92
bgIntelligenceInventoryFuzzyMinKeywordLength = 6
bgIntelligenceInventoryVisualMatchingEnabled = True
bgIntelligenceInventoryVisualCandidateLimit = 120
bgIntelligenceInventoryVisualReferenceLimit = 80
bgIntelligenceInventoryVisualHashDistanceMax = 3
bgIntelligenceInventoryVisualHashSize = 16
bgIntelligenceInventoryVisualColorMatchingEnabled = True
bgIntelligenceInventoryVisualColorDistanceMax = 0.58
bgIntelligenceGamepassMaxPages = 0
bgIntelligenceGamepassHardMaxPages = 100
bgIntelligenceBadgeHistoryPageSize = 100
bgIntelligenceBadgeHistoryMaxPages = 0
bgIntelligenceBadgeHistoryHardMaxPages = 100
bgIntelligencePrivateInventoryDmEnabled = True
bgIntelligenceReportRetentionHours = 24
bgIntelligenceReportIndexRetentionDays = 90
bgIntelligenceIdentityGraphRetentionDays = 365
bgIntelligenceReportPruneCheckIntervalSec = 3600
robloxApiCacheMaxEntries = 5000
robloxProfileCacheTtlSec = 86400
robloxGroupCacheTtlSec = 3600
robloxConnectionCacheTtlSec = 3600
robloxFriendListCacheTtlSec = 3600
robloxFavoriteGamesCacheTtlSec = 3600
robloxOutfitCacheTtlSec = 3600
robloxInventoryValueCacheTtlSec = 21600
robloxGamepassCacheTtlSec = 21600
robloxAssetPriceCacheTtlSec = 86400
robloxAssetThumbnailCacheTtlSec = 86400
robloxAssetThumbnailHashCacheTtlSec = 86400
robloxAssetPriceLookupConcurrency = 16
robloxAssetPriceFallbackMaxAssets = 10
robloxAssetThumbnailHashConcurrency = 10
robloxGamepassProductCacheTtlSec = 86400
robloxBadgeHistoryCacheTtlSec = 86400
robloxBadgeAwardCacheTtlSec = 86400
robloxBadgeAwardLookupConcurrency = 1
robloxBadgeAwardLookupDelaySec = 0.5
trainingResultsChannelId = 0
startupGreetingChannelId = 0
bgFailureForumChannelId = 0

# Training log mirror and John event ingest.
johnTrainingLogChannelId = 0
trainingArchiveChannelId = johnTrainingLogChannelId
trainingLogBackfillDays = 2
trainingLogStartupSyncDelaySec = 90
trainingLogStartupBackfillMessageLimit = 250
trainingLogArchiveIndexLimit = 500
orbatStartupMaintenanceDelaySec = 45
trainingSummaryWebhookName = "Jane Training Summary"
trainingMirrorWebhookName = "Jane Training Log"
johnEventLogChannelId = 0
johnClankerBotId = 0

honorGuardEnabled = False
honorGuardCommandGuildIds = []
honorGuardReviewChannelId = 0
honorGuardLogChannelId = 0
honorGuardArchiveChannelId = 0
honorGuardSpreadsheetId = _envText(
    "HONOR_GUARD_SPREADSHEET_ID",
    "1aLD68JNA2nRjTxG1c3DZOtb_DPRXE4uNKN7ZxpKo_0k",
)
honorGuardMemberSheetName = "Main"
honorGuardArchiveSheetName = "Event Archive"
honorGuardEventHostsSheetName = "Event Hosts"
honorGuardCredentialsPathEnvVar = "ORBAT_GOOGLE_CREDENTIALS_PATH"
honorGuardCredentialsPathConfigKey = "orbatGoogleCredentialsPath"

# Honor Guard member sheet columns.
honorGuardDiscordIdColumn = ""
honorGuardRobloxUsernameColumn = "A"
honorGuardRankColumn = "B"
honorGuardActivityStatusColumn = "H"
honorGuardQuotaPointsColumn = "E"
honorGuardPromotionEventPointsColumn = "K"
honorGuardPromotionAwardedPointsColumn = "L"
honorGuardPromotionTotalPointsColumn = ""
honorGuardJuniorExamPassedColumn = "O"
honorGuardNcoExamPassedColumn = "P"
honorGuardQuotaCompleteFormulaColumn = "G"
honorGuardPromotionEligibleFormulaColumn = "Q"
honorGuardStrikesColumn = "R"

# Honor Guard schedule/archive sheet columns.
honorGuardArchiveColumns = [
    "eventType",
    "eventTimeUtc",
    "host",
    "coHosts",
    "supervisors",
    "eventDuration",
    "eventDetail",
    "notes",
]
honorGuardEventHostUsernameColumn = "A"
honorGuardEventHostTotalEventsColumn = "F"
honorGuardEventHostExamsColumn = "G"
honorGuardEventHostTrainingsColumn = "H"
honorGuardEventHostTryoutsColumn = "I"
honorGuardEventHostInspectionsColumn = "J"
honorGuardEventHostEventTypeColumns = {
    "jge": honorGuardEventHostExamsColumn,
    "junior guardsman exam": honorGuardEventHostExamsColumn,
    "ncoe": honorGuardEventHostExamsColumn,
    "nco exam": honorGuardEventHostExamsColumn,
    "orientation": honorGuardEventHostTrainingsColumn,
    "training": honorGuardEventHostTrainingsColumn,
    "lecture": honorGuardEventHostTrainingsColumn,
    "drill": honorGuardEventHostTrainingsColumn,
    "tryout": honorGuardEventHostTryoutsColumn,
    "honor guard tryout": honorGuardEventHostTryoutsColumn,
    "inspection": honorGuardEventHostInspectionsColumn,
    "mock inspection": honorGuardEventHostInspectionsColumn,
}

# Honor Guard ranks and point rules.
honorGuardEnlistedRoleIds = []
honorGuardNcoRoleIds = []
honorGuardOfficerRoleIds = []

honorGuardExcuseStatusValues = [
    "Excused",
    "LoA",
    "LOA",
    "Retired",
    "N/A",
    "New",
]
honorGuardBiweeklyQuotaPointsRequired = 4
honorGuardEarlyActiveQuotaPoints = 8
honorGuardSoloSentryDutyMinutesRequired = 30
honorGuardSoloSentryDutyPromotionPoints = 1
honorGuardAttendanceQuotaPointsByEventType = {
    "gamenight": 0.5,
}
honorGuardAttendancePromotionPointsByEventType = {
    "drill" : {
        "base": 2,
        "per_intervall": 0,
        "minimum": 0
    },
    "inspection": {
        "base": 8,
        "per_intervall": 0,
        "minimum": 0
    },
    "sentry": {
        "base": 0,
        "per_intervall": 1,
        "minimum": 0
    },
    "orientation": {
        "base": 0,
        "per_intervall": 1,
        "minimum": 2
    },
}
honorGuardHostPromotionPointsByEventType = {
    "gamenight": 1,
    "orientation": 2,
    "drill": 3,
    "lecture": 3,
    "tryout": 6,
    "inspection": 8,
}
honorGuardSupervisorPromotionPointsByEventType = {
    "orientation": {
        "base": 0,
        "per_intervall": 1,
        "minimum": 2
    },
    "drill" : {
        "base": 2,
        "per_intervall": 0,
        "minimum": 0
    },
}
honorGuardJgePointsPerGradedAttendee = 0.75
honorGuardNcoExamPointsPerGradedAttendee = 1.5
honorGuardNcoExamScreenAssistPoints = 2

canCreateVoiceChatAll = [
    1376949984750206986,
    1399386519256563793,
    1416982954285989998,
    1376949919100698814
]
canCreateVoiceChatBasic = [
    1456604223407001601,
    1376949984750206986,
    1399386519256563793,
    1416982954285989998,
    1376949919100698814
]
