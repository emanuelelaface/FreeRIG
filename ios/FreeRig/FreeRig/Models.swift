import Foundation

struct RadioState: Codable, Equatable {
    var demo: Bool
    var ageS: Double
    var counters: RadioCounters?
    var main: String
    var left: RadioSideState
    var right: RadioSideState
    var activity: RadioActivityState?
    var mute: Bool
    var overlay: RadioOverlayState?
    var menu: RadioMenuState?
    var displaySettings: RadioDisplaySettings?
    var pttLatched: Bool
    var txAudioActive: Bool
    var save: SaveState?
    var human: String
    var radioPowered: Bool
    var poweringOn: Bool
    var powerMessage: String
    var powerGPIO: Int?
    var uartSelectGPIO: Int?
    var rxPowerAlive: Bool
    var rxPowerAgeS: Double?
    var rxPowerFrames: Int?
    var rxPowerTimeoutS: Double?
}

struct RadioCounters: Codable, Equatable {
    var frames: Int?
    var data: Int?
    var menuIgnored: Int?
    var ignored: Int?
    var syncLoss: Int?
}

struct RadioSideState: Codable, Equatable {
    var side: String
    var isMain: Bool
    var source: String
    var sourceCode: Int?
    var memGroup: String
    var memNo: String
    var name: String
    var freq: String
    var mode: String
    var modeRaw: String
    var shift: String
    var tone: String
    var rxActive: Bool
    var txActive: Bool
    var activityRaw: Int?
    var sMeterRaw: Int?
    var lower: RadioLowerState?
}

struct RadioLowerState: Codable, Equatable {
    var label: String
    var labelRaw: Int?
    var valueRaw: Int?
    var sqlRaw: Int?
    var volRaw: Int?
    var barRaw: Int?
    var barKind: String?
    var barMax: Int?
    var valueCandidateRaw: Int?
    var sideValueRaw: Int?
}

struct RadioActivityState: Codable, Equatable {
    var status: String
    var activityRaw: Int?
    var leftActivityRaw: Int?
    var rightActivityRaw: Int?
    var meterRaw: Int?
    var leftMeterRaw: Int?
    var rightMeterRaw: Int?
    var txFlag: Int?
    var rxFlag: Int?
    var rxRep: [Int]?
    var rxLeft: Bool
    var rxRight: Bool
    var rxAmbiguous: Bool?
    var txLeft: Bool
    var txRight: Bool
    var rxSides: [String]?
    var txSides: [String]?
    var side: String?
}

struct RadioOverlayState: Codable, Equatable {
    var active: Bool
    var kind: String
    var text: String?
    var latched: Bool?
    var title: String?
    var message: String?
    var options: [RadioOverlayOption]?
}

struct RadioOverlayOption: Codable, Equatable {
    var text: String
    var selected: Bool
}

struct RadioMenuState: Codable, Equatable {
    var visible: Bool
    var type: String?
    var ageS: Double?
    var title: String?
    var parentNum: Int?
    var category: String?
    var selectedRow: Int?
    var selectedIndex: Int?
    var selectedNum: Int?
    var selected: Int?
    var footer: String?
    var footerSelected: Bool?
    var assignment: Bool?
    var editing: Bool?
    var value: String?
    var valueSource: String?
    var valueSelected: Bool?
    var rawValue: String?
    var noActionItems: [Int]?
    var rows: [RadioMenuRow]?
    var cells: [RadioMenuCell]?
    var channels: [RadioPMGChannel]?
    var memoryRows: [RadioMenuRow]?
    var selectedMemoryRow: Int?
    var memoryNum: Int?
    var memoryFreq: String?
    var memoryName: String?
    var channelCount: Int?
    var source: String?
    var freq: String?
    var mode: String?
    var rxMode: String?
    var shift: String?
    var tone: String?
    var main: String?
    var auto: Bool?
    var rawBars: [Int]?
    var bars: [Int]?
    var width: String?
    var memoryMode: Bool?
    var markerRaw: Int?
    var markerIndex: Int?
    var interval: String?

    enum CodingKeys: String, CodingKey {
        case visible
        case type
        case ageS
        case title
        case parentNum
        case category
        case selectedRow
        case selectedIndex
        case selectedNum
        case selected
        case footer
        case footerSelected
        case assignment
        case editing
        case value
        case valueSource
        case valueSelected
        case rawValue
        case noActionItems
        case rows
        case cells
        case channels
        case memoryRows
        case selectedMemoryRow
        case memoryNum
        case memoryFreq
        case memoryName
        case source
        case freq
        case mode
        case rxMode
        case shift
        case tone
        case main
        case auto
        case rawBars
        case bars
        case width
        case memoryMode
        case markerRaw
        case markerIndex
        case interval
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        visible = try container.decode(Bool.self, forKey: .visible)
        type = try container.decodeIfPresent(String.self, forKey: .type)
        ageS = try container.decodeIfPresent(Double.self, forKey: .ageS)
        title = try container.decodeIfPresent(String.self, forKey: .title)
        parentNum = try container.decodeIfPresent(Int.self, forKey: .parentNum)
        category = try container.decodeIfPresent(String.self, forKey: .category)
        selectedRow = try container.decodeIfPresent(Int.self, forKey: .selectedRow)
        selectedIndex = try container.decodeIfPresent(Int.self, forKey: .selectedIndex)
        selectedNum = try container.decodeIfPresent(Int.self, forKey: .selectedNum)
        selected = try container.decodeIfPresent(Int.self, forKey: .selected)
        footer = try container.decodeIfPresent(String.self, forKey: .footer)
        footerSelected = try container.decodeIfPresent(Bool.self, forKey: .footerSelected)
        assignment = try container.decodeIfPresent(Bool.self, forKey: .assignment)
        editing = try container.decodeIfPresent(Bool.self, forKey: .editing)
        value = try container.decodeIfPresent(String.self, forKey: .value)
        valueSource = try container.decodeIfPresent(String.self, forKey: .valueSource)
        valueSelected = try container.decodeIfPresent(Bool.self, forKey: .valueSelected)
        rawValue = try container.decodeIfPresent(String.self, forKey: .rawValue)
        noActionItems = try container.decodeIfPresent([Int].self, forKey: .noActionItems)
        rows = try container.decodeIfPresent([RadioMenuRow].self, forKey: .rows)
        cells = try container.decodeIfPresent([RadioMenuCell].self, forKey: .cells)
        if let pmgChannels = try? container.decode([RadioPMGChannel].self, forKey: .channels) {
            channels = pmgChannels
            channelCount = pmgChannels.count
        } else {
            channels = nil
            channelCount = try? container.decode(Int.self, forKey: .channels)
        }
        memoryRows = try container.decodeIfPresent([RadioMenuRow].self, forKey: .memoryRows)
        selectedMemoryRow = try container.decodeIfPresent(Int.self, forKey: .selectedMemoryRow)
        memoryNum = try container.decodeIfPresent(Int.self, forKey: .memoryNum)
        memoryFreq = try container.decodeIfPresent(String.self, forKey: .memoryFreq)
        memoryName = try container.decodeIfPresent(String.self, forKey: .memoryName)
        source = try container.decodeIfPresent(String.self, forKey: .source)
        freq = try container.decodeIfPresent(String.self, forKey: .freq)
        mode = try container.decodeIfPresent(String.self, forKey: .mode)
        rxMode = try container.decodeIfPresent(String.self, forKey: .rxMode)
        shift = try container.decodeIfPresent(String.self, forKey: .shift)
        tone = try container.decodeIfPresent(String.self, forKey: .tone)
        main = try container.decodeIfPresent(String.self, forKey: .main)
        auto = try container.decodeIfPresent(Bool.self, forKey: .auto)
        rawBars = try container.decodeIfPresent([Int].self, forKey: .rawBars)
        bars = try container.decodeIfPresent([Int].self, forKey: .bars)
        width = try container.decodeIfPresent(String.self, forKey: .width)
        memoryMode = try container.decodeIfPresent(Bool.self, forKey: .memoryMode)
        markerRaw = try container.decodeIfPresent(Int.self, forKey: .markerRaw)
        markerIndex = try container.decodeIfPresent(Int.self, forKey: .markerIndex)
        interval = try container.decodeIfPresent(String.self, forKey: .interval)
    }
}

struct RadioMenuRow: Codable, Equatable, Identifiable {
    var id: String {
        if let num, !num.isEmpty { return num }
        return "\(label ?? text ?? value ?? UUID().uuidString)"
    }

    var row: Int?
    var num: String?
    var text: String?
    var label: String?
    var value: String?
    var editing: Bool?
    var freq: String?
    var name: String?

    enum CodingKeys: String, CodingKey {
        case row
        case num
        case text
        case label
        case value
        case editing
        case freq
        case name
    }

    init(
        row: Int? = nil,
        num: String? = nil,
        text: String? = nil,
        label: String? = nil,
        value: String? = nil,
        editing: Bool? = nil,
        freq: String? = nil,
        name: String? = nil
    ) {
        self.row = row
        self.num = num
        self.text = text
        self.label = label
        self.value = value
        self.editing = editing
        self.freq = freq
        self.name = name
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        row = try container.decodeIfPresent(Int.self, forKey: .row)
        if let stringNum = try? container.decode(String.self, forKey: .num) {
            num = stringNum
        } else if let intNum = try? container.decode(Int.self, forKey: .num) {
            num = String(intNum)
        } else {
            num = nil
        }
        text = try container.decodeIfPresent(String.self, forKey: .text)
        label = try container.decodeIfPresent(String.self, forKey: .label)
        value = try container.decodeIfPresent(String.self, forKey: .value)
        editing = try container.decodeIfPresent(Bool.self, forKey: .editing)
        freq = try container.decodeIfPresent(String.self, forKey: .freq)
        name = try container.decodeIfPresent(String.self, forKey: .name)
    }
}

struct RadioMenuCell: Codable, Equatable, Identifiable {
    var id: Int { index ?? 0 }
    var index: Int?
    var text: String?
}

struct RadioPMGChannel: Codable, Equatable, Identifiable {
    var id: Int { index }
    var index: Int
    var label: String
    var registered: Bool
    var bar: Int?
    var shadow: Int?
    var recent: Bool?
    var receiving: Bool?
}

struct RadioDisplaySettings: Codable, Equatable {
    var lcdDimmer: String?
    var lcdContrast: Int?
    var sMeterSymbol: String?
    var backlightColor: String?
    var bandScope: String?
}

struct SaveState: Codable, Equatable {
    var active: Bool
    var root: String?
    var label: String?
    var startedAt: Double?
    var elapsedS: Double?
    var events: Int?
    var screens: Int?
    var commands: Int?
    var lastPath: String?
    var zipPath: String?
}

struct AudioStateResponse: Codable, Equatable {
    var enabled: Bool?
    var device: String?
    var rate: Int?
    var channels: Int?
    var format: String?
    var chunkMs: Int?
    var running: Bool?
    var lastError: String?
    var rx: AudioStreamState?
    var tx: AudioTXState?
    var ptt: PTTState?
}

struct AudioStreamState: Codable, Equatable {
    var enabled: Bool
    var device: String?
    var rate: Int?
    var channels: Int?
    var format: String?
    var chunkMs: Int?
    var running: Bool?
    var lastError: String?
}

struct AudioTXState: Codable, Equatable {
    var enabled: Bool
    var device: String?
    var rate: Int?
    var channels: Int?
    var playbackChannels: Int?
    var bufferTimeUs: Int?
    var periodTimeUs: Int?
    var pttLeadMs: Int?
    var pttTailMs: Int?
    var processorSize: Int?
    var maxWsBufferBytes: Int?
    var outputGain: Double?
    var agcEnabled: Bool?
    var agcTarget: Double?
    var agcMaxBoost: Double?
    var agcCurrentBoost: Double?
    var alsaMessage: String?
    var aplayCommand: String?
    var active: Bool?
    var running: Bool?
    var ageS: Double?
    var bytesReceived: Int?
    var chunksReceived: Int?
    var inputPeak: Double?
    var outputPeak: Double?
    var outputRMS: Double?
    var totalGain: Double?
    var lastError: String?
    var clippedSamples: Int?
    var totalClippedSamples: Int?
    var lastChunkSamples: Int?
    var meterAgeS: Double?
}

struct PTTState: Codable, Equatable {
    var mode: String
    var active: Bool
    var lastError: String?
}

struct CommandResponse: Codable, Equatable {
    var ok: Bool
    var message: String?
    var error: String?
    var pttLatched: Bool?
}

struct StateSocketEnvelope: Codable {
    var type: String
    var state: RadioState?
    var version: Int?
    var transport: String?
    var intervalMs: Int?
    var heartbeatS: Double?
}

extension JSONDecoder {
    static let radioAPI: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()
}
