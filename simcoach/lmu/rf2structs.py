"""ctypes structs for LMU's live telemetry/scoring shared memory.

VERIFIED (transcribed field-by-field from the on-disk header, exact order +
types): TelemVect3, TelemWheelV01, TelemInfoV01, VehicleScoringInfoV01, from
  S:\\SteamLibrary\\steamapps\\common\\Le Mans Ultimate\\Support\\SharedMemoryInterface\\InternalsPlugin.hpp
That header wraps everything in `#pragma pack(push, 4)` (line 24) through
`#pragma pack(pop)` (line 1106) -- i.e. 4-byte packing, NOT natural 8-byte
alignment. Every struct below sets `_pack_ = 4` to match. (This corrects an
assumption in the task brief, which expected natural alignment.)

NOT verified against any local file tonight (LMU was not running, and the
Iron Wolf rF2SharedMemoryMapPlugin's own header, e.g. rF2State.h, is not
present in this install -- only ISI's InternalsPlugin.hpp is):
  - rF2Telemetry / rF2Scoring wrapper preambles (mVersionUpdateBegin/End,
    mBytesUpdatedHint, mNumVehicles) follow the public Iron Wolf plugin
    convention from memory, not a local header.
  - rF2ScoringInfo: only transcribed through mNumVehicles (from ISI's
    ScoringInfoV01 order), with the `char *mResultsStream` pointer field
    replaced by an 8-byte placeholder (a pointer isn't meaningful across a
    shared-memory boundary; unknown what the plugin author did instead).
    `_tail_unvalidated` is a 0-byte placeholder -- we have NO evidence for
    the true gap between mNumVehicles and mVehicles[] in the real plugin
    struct. Scoring vehicle lookups are UNRELIABLE until this is fixed.

Everything here is pending LIVE validation -- see HANDOFF [LUNCH]: confirm
offsets, struct sizes, and sign conventions against a running sim.
"""
import ctypes as C

MAX_MAPPED_VEHICLES = 128  # rF2 plugin convention; not confirmed locally


class TelemVect3(C.Structure):
    _pack_ = 4
    _fields_ = [("x", C.c_double), ("y", C.c_double), ("z", C.c_double)]

    def magnitude(self) -> float:
        return (self.x ** 2 + self.y ** 2 + self.z ** 2) ** 0.5


class TelemWheelV01(C.Structure):
    _pack_ = 4
    _fields_ = [
        ("mSuspensionDeflection", C.c_double),
        ("mRideHeight", C.c_double),
        ("mSuspForce", C.c_double),
        ("mBrakeTemp", C.c_double),
        ("mBrakePressure", C.c_double),
        ("mRotation", C.c_double),
        ("mLateralPatchVel", C.c_double),
        ("mLongitudinalPatchVel", C.c_double),
        ("mLateralGroundVel", C.c_double),
        ("mLongitudinalGroundVel", C.c_double),
        ("mCamber", C.c_double),
        ("mLateralForce", C.c_double),
        ("mLongitudinalForce", C.c_double),
        ("mTireLoad", C.c_double),
        ("mGripFract", C.c_double),
        ("mPressure", C.c_double),
        ("mTemperature", C.c_double * 3),  # Kelvin; left/center/right
        ("mWear", C.c_double),
        ("mTerrainName", C.c_char * 16),
        ("mSurfaceType", C.c_ubyte),
        ("mFlat", C.c_bool),
        ("mDetached", C.c_bool),
        ("mStaticUndeflectedRadius", C.c_ubyte),
        ("mVerticalTireDeflection", C.c_double),
        ("mWheelYLocation", C.c_double),
        ("mToe", C.c_double),
        ("mTireCarcassTemperature", C.c_double),
        ("mTireInnerLayerTemperature", C.c_double * 3),
        ("mOptimalTemp", C.c_float),
        ("mCompoundIndex", C.c_ubyte),
        ("mCompoundType", C.c_ubyte),
        ("mExpansion", C.c_ubyte * 18),
    ]


class TelemInfoV01(C.Structure):
    """== rF2VehicleTelemetry (per-vehicle element of rF2Telemetry.mVehicles)."""

    _pack_ = 4
    _fields_ = [
        ("mID", C.c_long),
        ("mDeltaTime", C.c_double),
        ("mElapsedTime", C.c_double),
        ("mLapNumber", C.c_long),
        ("mLapStartET", C.c_double),
        ("mVehicleName", C.c_char * 64),
        ("mTrackName", C.c_char * 64),
        ("mPos", TelemVect3),
        ("mLocalVel", TelemVect3),
        ("mLocalAccel", TelemVect3),
        ("mOri", TelemVect3 * 3),
        ("mLocalRot", TelemVect3),
        ("mLocalRotAccel", TelemVect3),
        ("mGear", C.c_long),
        ("mEngineRPM", C.c_double),
        ("mEngineWaterTemp", C.c_double),
        ("mEngineOilTemp", C.c_double),
        ("mClutchRPM", C.c_double),
        ("mUnfilteredThrottle", C.c_double),
        ("mUnfilteredBrake", C.c_double),
        ("mUnfilteredSteering", C.c_double),
        ("mUnfilteredClutch", C.c_double),
        ("mFilteredThrottle", C.c_double),
        ("mFilteredBrake", C.c_double),
        ("mFilteredSteering", C.c_double),
        ("mFilteredClutch", C.c_double),
        ("mSteeringShaftTorque", C.c_double),
        ("mFront3rdDeflection", C.c_double),
        ("mRear3rdDeflection", C.c_double),
        ("mFrontWingHeight", C.c_double),
        ("mFrontRideHeight", C.c_double),
        ("mRearRideHeight", C.c_double),
        ("mDrag", C.c_double),
        ("mFrontDownforce", C.c_double),
        ("mRearDownforce", C.c_double),
        ("mFuel", C.c_double),
        ("mEngineMaxRPM", C.c_double),
        ("mScheduledStops", C.c_ubyte),
        ("mOverheating", C.c_bool),
        ("mDetached", C.c_bool),
        ("mHeadlights", C.c_bool),
        ("mDentSeverity", C.c_ubyte * 8),
        ("mLastImpactET", C.c_double),
        ("mLastImpactMagnitude", C.c_double),
        ("mLastImpactPos", TelemVect3),
        ("mEngineTorque", C.c_double),
        ("mCurrentSector", C.c_long),
        ("mSpeedLimiter", C.c_ubyte),
        ("mMaxGears", C.c_ubyte),
        ("mFrontTireCompoundIndex", C.c_ubyte),
        ("mRearTireCompoundIndex", C.c_ubyte),
        ("mFuelCapacity", C.c_double),
        ("mFrontFlapActivated", C.c_ubyte),
        ("mRearFlapActivated", C.c_ubyte),
        ("mRearFlapLegalStatus", C.c_ubyte),
        ("mIgnitionStarter", C.c_ubyte),
        ("mFrontTireCompoundName", C.c_char * 18),
        ("mRearTireCompoundName", C.c_char * 18),
        ("mSpeedLimiterAvailable", C.c_ubyte),
        ("mAntiStallActivated", C.c_ubyte),
        ("mUnused", C.c_ubyte * 2),
        ("mVisualSteeringWheelRange", C.c_float),
        ("mRearBrakeBias", C.c_double),
        ("mTurboBoostPressure", C.c_double),
        ("mPhysicsToGraphicsOffset", C.c_float * 3),
        ("mPhysicalSteeringWheelRange", C.c_float),
        ("mDeltaBest", C.c_double),
        ("mBatteryChargeFraction", C.c_double),
        ("mElectricBoostMotorTorque", C.c_double),
        ("mElectricBoostMotorRPM", C.c_double),
        ("mElectricBoostMotorTemperature", C.c_double),
        ("mElectricBoostWaterTemperature", C.c_double),
        ("mElectricBoostMotorState", C.c_ubyte),
        ("mLapInvalidated", C.c_bool),
        ("mABSActive", C.c_bool),
        ("mTCActive", C.c_bool),
        ("mSpeedLimiterActive", C.c_bool),
        ("mWiperState", C.c_uint8),
        ("mTC", C.c_uint8),
        ("mTCMax", C.c_uint8),
        ("mTCSlip", C.c_uint8),
        ("mTCSlipMax", C.c_uint8),
        ("mTCCut", C.c_uint8),
        ("mTCCutMax", C.c_uint8),
        ("mABS", C.c_uint8),
        ("mABSMax", C.c_uint8),
        ("mMotorMap", C.c_uint8),
        ("mMotorMapMax", C.c_uint8),
        ("mMigration", C.c_uint8),
        ("mMigrationMax", C.c_uint8),
        ("mFrontAntiSway", C.c_uint8),
        ("mFrontAntiSwayMax", C.c_uint8),
        ("mRearAntiSway", C.c_uint8),
        ("mRearAntiSwayMax", C.c_uint8),
        ("mLiftAndCoastProgress", C.c_uint8),
        ("mTrackLimitsSteps", C.c_uint8),
        ("mRegen", C.c_float),
        ("mSoC", C.c_float),
        ("mVirtualEnergy", C.c_float),
        ("mTimeGapCarAhead", C.c_float),
        ("mTimeGapCarBehind", C.c_float),
        ("mTimeGapPlaceAhead", C.c_float),
        ("mTimeGapPlaceBehind", C.c_float),
        ("mVehicleModel", C.c_char * 30),
        ("mVehicleClass", C.c_uint8),  # IP_VehicleClass : uint8_t
        ("mVehicleChampionship", C.c_uint8),  # IP_VehicleChampionship : uint8_t
        ("mExpansion", C.c_ubyte * 20),
        ("mWheel", TelemWheelV01 * 4),  # FL, FR, RL, RR
    ]


class VehicleScoringInfoV01(C.Structure):
    """== rF2VehicleScoring (per-vehicle element of rF2Scoring.mVehicles)."""

    _pack_ = 4
    _fields_ = [
        ("mID", C.c_long),
        ("mDriverName", C.c_char * 32),
        ("mVehicleName", C.c_char * 64),
        ("mTotalLaps", C.c_short),
        ("mSector", C.c_byte),
        ("mFinishStatus", C.c_byte),
        ("mLapDist", C.c_double),
        ("mPathLateral", C.c_double),
        ("mTrackEdge", C.c_double),
        ("mBestSector1", C.c_double),
        ("mBestSector2", C.c_double),
        ("mBestLapTime", C.c_double),
        ("mLastSector1", C.c_double),
        ("mLastSector2", C.c_double),
        ("mLastLapTime", C.c_double),
        ("mCurSector1", C.c_double),
        ("mCurSector2", C.c_double),
        ("mNumPitstops", C.c_short),
        ("mNumPenalties", C.c_short),
        ("mIsPlayer", C.c_bool),
        ("mControl", C.c_byte),
        ("mInPits", C.c_bool),
        ("mPlace", C.c_ubyte),
        ("mVehicleClass", C.c_char * 32),
        ("mTimeBehindNext", C.c_double),
        ("mLapsBehindNext", C.c_long),
        ("mTimeBehindLeader", C.c_double),
        ("mLapsBehindLeader", C.c_long),
        ("mLapStartET", C.c_double),
        ("mPos", TelemVect3),
        ("mLocalVel", TelemVect3),
        ("mLocalAccel", TelemVect3),
        ("mOri", TelemVect3 * 3),
        ("mLocalRot", TelemVect3),
        ("mLocalRotAccel", TelemVect3),
        ("mHeadlights", C.c_ubyte),
        ("mPitState", C.c_ubyte),
        ("mServerScored", C.c_ubyte),
        ("mIndividualPhase", C.c_ubyte),
        ("mQualification", C.c_long),
        ("mTimeIntoLap", C.c_double),
        ("mEstimatedLapTime", C.c_double),
        ("mPitGroup", C.c_char * 24),
        ("mFlag", C.c_ubyte),
        ("mUnderYellow", C.c_bool),
        ("mCountLapFlag", C.c_ubyte),
        ("mInGarageStall", C.c_bool),
        ("mUpgradePack", C.c_ubyte * 16),
        ("mPitLapDist", C.c_float),
        ("mBestLapSector1", C.c_float),
        ("mBestLapSector2", C.c_float),
        ("mSteamID", C.c_uint64),
        ("mVehFilename", C.c_char * 32),
        ("mAttackMode", C.c_short),
        ("mFuelFraction", C.c_ubyte),
        ("mDRSState", C.c_bool),
        ("mExpansion", C.c_ubyte * 4),
    ]


# Aliases matching the Iron Wolf rF2SMMP naming used in the task brief.
rF2VehicleTelemetry = TelemInfoV01
rF2VehicleScoring = VehicleScoringInfoV01


class rF2ScoringInfo(C.Structure):
    """UNVERIFIED, minimal (see module docstring). Only the prefix through
    mNumVehicles is transcribed; `_tail_unvalidated` is a 0-byte placeholder
    for the unknown remainder of the real struct before mVehicles[] starts.
    [LUNCH]: this is the single riskiest offset in this module."""

    _pack_ = 4
    _fields_ = [
        ("mTrackName", C.c_char * 64),
        ("mSession", C.c_long),
        ("mCurrentET", C.c_double),
        ("mEndET", C.c_double),
        ("mMaxLaps", C.c_long),
        ("mLapDist", C.c_double),
        ("_mResultsStream_ptr_or_pad", C.c_uint64),  # UNSURE: header has char*
        ("mNumVehicles", C.c_long),
        ("_tail_unvalidated", C.c_ubyte * 0),  # UNSURE: see docstring, [LUNCH]
    ]


class rF2Telemetry(C.Structure):
    """$rFactor2SMMP_Telemetry$ buffer. Wrapper preamble UNVERIFIED (public
    Iron Wolf plugin convention); mVehicles element type IS verified."""

    _pack_ = 4
    _fields_ = [
        ("mVersionUpdateBegin", C.c_uint32),
        ("mVersionUpdateEnd", C.c_uint32),
        ("mBytesUpdatedHint", C.c_int32),
        ("mNumVehicles", C.c_int32),
        ("mVehicles", rF2VehicleTelemetry * MAX_MAPPED_VEHICLES),
    ]


class rF2Scoring(C.Structure):
    """$rFactor2SMMP_Scoring$ buffer. Wrapper preamble and mScoringInfo are
    UNVERIFIED (see rF2ScoringInfo docstring); mVehicles element type IS
    verified."""

    _pack_ = 4
    _fields_ = [
        ("mVersionUpdateBegin", C.c_uint32),
        ("mVersionUpdateEnd", C.c_uint32),
        ("mBytesUpdatedHint", C.c_int32),
        ("mScoringInfo", rF2ScoringInfo),
        ("mVehicles", rF2VehicleScoring * MAX_MAPPED_VEHICLES),
    ]
