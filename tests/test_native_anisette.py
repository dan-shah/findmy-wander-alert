from native_anisette import needs_2fa

PET = {"t": {"com.apple.gs.idms.pet": {"token": "x"}}}


def test_au_signal():
    assert needs_2fa({"au": "secondaryAuth"}, PET) == "secondaryAuth"
    assert needs_2fa({"au": "trustedDeviceSecondaryAuth"}, {}) == "trustedDeviceSecondaryAuth"


def test_hsc_409_without_au_means_trusted_device_2fa():
    assert needs_2fa({"ec": 0, "hsc": 409}, PET) == "trustedDeviceSecondaryAuth"
    assert needs_2fa({"ec": 0, "hsc": "409"}, PET) == "trustedDeviceSecondaryAuth"


def test_missing_pet_means_2fa():
    assert needs_2fa({"ec": 0, "hsc": 200}, {"t": {}}) == "trustedDeviceSecondaryAuth"


def test_fully_authenticated():
    assert needs_2fa({"ec": 0, "hsc": 200}, PET) is None
