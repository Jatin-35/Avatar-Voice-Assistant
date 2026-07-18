"""
voice/stt.py — Web STT handler based on DeepgramSTTHandler from parent src/.

Subclasses DeepgramSTTHandler to override _PARAMS for 16kHz web audio
(browser AudioWorklet captures at 16kHz instead of 8kHz used by Cube VoIP).

Also exports VESTIGE_KEYTERMS (copied from streaming_cube.py) and a factory
function create_stt_handler().
"""

from shared.deepgram_stt import DeepgramSTTHandler
from config import DEEPGRAM_API_KEY
from typing import Optional, Callable

# ── Vestige-specific STT keyterms ─────────────────────────────────────────────
VESTIGE_KEYTERMS = [
    # Brand & bot
    "Vestige", "Vani",

    # Nine ways to earn — phrase boosts
    "Performance Bonus", "Director Bonus", "Team Building Bonus",
    "Leadership Overriding Bonus", "Travel Fund", "Car Fund", "House Fund",
    "Elite Club Bonus", "Savings on Consumption", "Accumulative Plan",

    # Plan metrics
    "PV", "BV", "MRP", "DP",
    "Point Value", "Business Volume", "Group PV", "PV slab",

    # Ranks
    "Bronze Director", "Silver Director", "Gold Director", "Star Director",
    "Diamond Director", "Crown Director", "Universal Crown",
    "Double Crown", "Double Universal Crown",

    # Onboarding / network
    "DLCP", "Mini DLCP",
    "distributor", "upline", "downline", "sponsor", "referral",

    # Product brand & categories
    "Mistral of Milan", "Wellness", "Nutrition", "Personal Care",
    "Bio Fertiliser", "FSSAI", "Halal",
    "LITE HOUSE", "Rice Bran Oil", "Assure Soap", "Dentassure Toothpaste",

    # Common business words
    "Monthly Scheme", "scheme", "purchase", "minimum purchase",
    "cashback", "incentive",

    # Devanagari forms
    "वेस्टीज", "वाणी",
    "डिस्ट्रिब्यूटर", "डायरेक्टर", "डायमंड", "क्राउन",
    "बोनस", "स्कीम", "प्लान", "लेवल",
    "पीवी", "बीवी",
    "अपलाइन", "डाउनलाइन", "प्रोडक्ट",

    # Acronyms
    "TBB", "LOB", "GPV", "DAF", "KYC",

    # Brand & marketing extras
    "Vestige Marketing Plan", "Vestige Distributor", "Distributorship",
    "Compensation Plan", "Fast Start Bronze Director",

    # Plan mechanics
    "Self Purchase", "PV BV Ratio", "Retail Profit",

    # Distributor lifecycle
    "Distributor Application Form", "Distributor Agreement",
    "Inactive Distributor", "Cross Sponsoring", "Line of Sponsorship",

    # Policy phrases
    "Buy Back Policy", "Product Return Policy", "Succession Policy",

    # Cosmetic brand specifics
    "Skin Formula 9", "Perfect Blend Liquid Foundation",
    "Advanced Luminous Technology",
    "Cruelty Free", "Vegan Products", "Parabens",
    "Velvety Matte Finish", "Oil Absorbing Powder",

    # Agri compliance
    "Bio Fertiliser Licence", "Carrier Based Consortia", "Fertiliser Office",
]


class WebDeepgramSTTHandler(DeepgramSTTHandler):
    """DeepgramSTTHandler configured for 16kHz web audio (browser AudioWorklet).

    The only change vs. the phone-bot version is sample_rate=16000 instead of 8000.
    All other behaviour (nova-3, multi-language, keyterms, VAD signals) is identical.
    """

    # Override _PARAMS from parent: change sample_rate to 16000
    _PARAMS = {
        "model":            "nova-3",
        "language":         "multi",
        "encoding":         "linear16",
        "sample_rate":      16000,        # Web: browser AudioWorklet at 16kHz
        "channels":         1,
        "interim_results":  "true",
        "smart_format":     "false",
        "punctuate":        "false",
        "endpointing":      "500",          # 500ms of silence ends a turn (speech_final)
        "utterance_end_ms": "1000",
        "vad_events":       "true",
    }


def create_stt_handler(
    session_id: str,
    on_recognized: Optional[Callable] = None,
    on_recognizing: Optional[Callable] = None,
    on_speech_started: Optional[Callable] = None,
) -> WebDeepgramSTTHandler:
    """Factory: create a WebDeepgramSTTHandler for a given session.

    Args:
        session_id: Used for logging only (not sent to Deepgram)
        on_recognized: Callback for final transcripts
        on_recognizing: Callback for partial transcripts
        on_speech_started: Callback for VAD speech-start (drives barge-in)

    Returns:
        Configured WebDeepgramSTTHandler (not yet started)
    """
    print(f"[STT] Creating WebDeepgramSTTHandler for session {session_id} (16kHz)")
    return WebDeepgramSTTHandler(
        api_key=DEEPGRAM_API_KEY,
        on_recognized=on_recognized,
        on_recognizing=on_recognizing,
        on_speech_started=on_speech_started,
        keywords=VESTIGE_KEYTERMS,
    )
