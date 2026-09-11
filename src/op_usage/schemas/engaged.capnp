# Minimal cereal Event overlay for qlog engaged-time parsing.
#
# Matches commaai/openpilot cereal/log.capnp (master, 2026-09):
#   Event.logMonoTime @0
#   Event.valid @67          # NOT a union member — putting it in the union
#                            # shifts the discriminant so selfdriveState @130
#                            # is reported as u129 and never sampled.
#   Event.union:
#     controlsState @7
#     selfdriveState @130
#     (placeholders through @160; cereal currently tops out at @152)
#   SelfdriveState.enabled @1
#   ControlsState.enabled @19
#     (cereal nested this under deprecated :group; same ordinal / wire bit)
#
# Unused union members are AnyPointer placeholders so the capnp compiler
# accepts consecutive union ordinals (with the required @67 hole).
# Do not treat this file as cereal.

@0xbfa5e2c1d4a80917;

struct SelfdriveState {
  state @0 :UInt16;
  enabled @1 :Bool;
  active @2 :Bool;
}

struct ControlsState {
  vEgo @0 :Float32;
  aEgo @1 :Float32;
  vPid @2 :Float32;
  vTargetLead @3 :Float32;
  upAccelCmd @4 :Float32;
  uiAccelCmd @5 :Float32;
  yActual @6 :Float32;
  yDes @7 :Float32;
  upSteer @8 :Float32;
  uiSteer @9 :Float32;
  aTargetMin @10 :Float32;
  aTargetMax @11 :Float32;
  jerkFactor @12 :Float32;
  angleSteers @13 :Float32;
  hudLead @14 :Int32;
  cumLagMs @15 :Float32;
  canMonoTime @16 :UInt64;
  radarStateMonoTime @17 :UInt64;
  mdMonoTime @18 :UInt64;
  enabled @19 :Bool;
}

struct Event {
  logMonoTime @0 :UInt64;
  valid @67 :Bool = true;
  union {
    u1 @1 :AnyPointer;
    u2 @2 :AnyPointer;
    u3 @3 :AnyPointer;
    u4 @4 :AnyPointer;
    u5 @5 :AnyPointer;
    u6 @6 :AnyPointer;
    controlsState @7 :ControlsState;
    u8 @8 :AnyPointer;
    u9 @9 :AnyPointer;
    u10 @10 :AnyPointer;
    u11 @11 :AnyPointer;
    u12 @12 :AnyPointer;
    u13 @13 :AnyPointer;
    u14 @14 :AnyPointer;
    u15 @15 :AnyPointer;
    u16 @16 :AnyPointer;
    u17 @17 :AnyPointer;
    u18 @18 :AnyPointer;
    u19 @19 :AnyPointer;
    u20 @20 :AnyPointer;
    u21 @21 :AnyPointer;
    u22 @22 :AnyPointer;
    u23 @23 :AnyPointer;
    u24 @24 :AnyPointer;
    u25 @25 :AnyPointer;
    u26 @26 :AnyPointer;
    u27 @27 :AnyPointer;
    u28 @28 :AnyPointer;
    u29 @29 :AnyPointer;
    u30 @30 :AnyPointer;
    u31 @31 :AnyPointer;
    u32 @32 :AnyPointer;
    u33 @33 :AnyPointer;
    u34 @34 :AnyPointer;
    u35 @35 :AnyPointer;
    u36 @36 :AnyPointer;
    u37 @37 :AnyPointer;
    u38 @38 :AnyPointer;
    u39 @39 :AnyPointer;
    u40 @40 :AnyPointer;
    u41 @41 :AnyPointer;
    u42 @42 :AnyPointer;
    u43 @43 :AnyPointer;
    u44 @44 :AnyPointer;
    u45 @45 :AnyPointer;
    u46 @46 :AnyPointer;
    u47 @47 :AnyPointer;
    u48 @48 :AnyPointer;
    u49 @49 :AnyPointer;
    u50 @50 :AnyPointer;
    u51 @51 :AnyPointer;
    u52 @52 :AnyPointer;
    u53 @53 :AnyPointer;
    u54 @54 :AnyPointer;
    u55 @55 :AnyPointer;
    u56 @56 :AnyPointer;
    u57 @57 :AnyPointer;
    u58 @58 :AnyPointer;
    u59 @59 :AnyPointer;
    u60 @60 :AnyPointer;
    u61 @61 :AnyPointer;
    u62 @62 :AnyPointer;
    u63 @63 :AnyPointer;
    u64 @64 :AnyPointer;
    u65 @65 :AnyPointer;
    u66 @66 :AnyPointer;
    u68 @68 :AnyPointer;
    u69 @69 :AnyPointer;
    u70 @70 :AnyPointer;
    u71 @71 :AnyPointer;
    u72 @72 :AnyPointer;
    u73 @73 :AnyPointer;
    u74 @74 :AnyPointer;
    u75 @75 :AnyPointer;
    u76 @76 :AnyPointer;
    u77 @77 :AnyPointer;
    u78 @78 :AnyPointer;
    u79 @79 :AnyPointer;
    u80 @80 :AnyPointer;
    u81 @81 :AnyPointer;
    u82 @82 :AnyPointer;
    u83 @83 :AnyPointer;
    u84 @84 :AnyPointer;
    u85 @85 :AnyPointer;
    u86 @86 :AnyPointer;
    u87 @87 :AnyPointer;
    u88 @88 :AnyPointer;
    u89 @89 :AnyPointer;
    u90 @90 :AnyPointer;
    u91 @91 :AnyPointer;
    u92 @92 :AnyPointer;
    u93 @93 :AnyPointer;
    u94 @94 :AnyPointer;
    u95 @95 :AnyPointer;
    u96 @96 :AnyPointer;
    u97 @97 :AnyPointer;
    u98 @98 :AnyPointer;
    u99 @99 :AnyPointer;
    u100 @100 :AnyPointer;
    u101 @101 :AnyPointer;
    u102 @102 :AnyPointer;
    u103 @103 :AnyPointer;
    u104 @104 :AnyPointer;
    u105 @105 :AnyPointer;
    u106 @106 :AnyPointer;
    u107 @107 :AnyPointer;
    u108 @108 :AnyPointer;
    u109 @109 :AnyPointer;
    u110 @110 :AnyPointer;
    u111 @111 :AnyPointer;
    u112 @112 :AnyPointer;
    u113 @113 :AnyPointer;
    u114 @114 :AnyPointer;
    u115 @115 :AnyPointer;
    u116 @116 :AnyPointer;
    u117 @117 :AnyPointer;
    u118 @118 :AnyPointer;
    u119 @119 :AnyPointer;
    u120 @120 :AnyPointer;
    u121 @121 :AnyPointer;
    u122 @122 :AnyPointer;
    u123 @123 :AnyPointer;
    u124 @124 :AnyPointer;
    u125 @125 :AnyPointer;
    u126 @126 :AnyPointer;
    u127 @127 :AnyPointer;
    u128 @128 :AnyPointer;
    u129 @129 :AnyPointer;
    selfdriveState @130 :SelfdriveState;
    u131 @131 :AnyPointer;
    u132 @132 :AnyPointer;
    u133 @133 :AnyPointer;
    u134 @134 :AnyPointer;
    u135 @135 :AnyPointer;
    u136 @136 :AnyPointer;
    u137 @137 :AnyPointer;
    u138 @138 :AnyPointer;
    u139 @139 :AnyPointer;
    u140 @140 :AnyPointer;
    u141 @141 :AnyPointer;
    u142 @142 :AnyPointer;
    u143 @143 :AnyPointer;
    u144 @144 :AnyPointer;
    u145 @145 :AnyPointer;
    u146 @146 :AnyPointer;
    u147 @147 :AnyPointer;
    u148 @148 :AnyPointer;
    u149 @149 :AnyPointer;
    u150 @150 :AnyPointer;
    u151 @151 :AnyPointer;
    u152 @152 :AnyPointer;
    u153 @153 :AnyPointer;
    u154 @154 :AnyPointer;
    u155 @155 :AnyPointer;
    u156 @156 :AnyPointer;
    u157 @157 :AnyPointer;
    u158 @158 :AnyPointer;
    u159 @159 :AnyPointer;
    u160 @160 :AnyPointer;
  }
}
