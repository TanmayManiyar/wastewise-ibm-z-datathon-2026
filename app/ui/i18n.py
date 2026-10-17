"""Crew phone page texts in English, Marathi and Hindi."""

LANGS = {"en": "English", "mr": "मराठी", "hi": "हिंदी"}

T = {
    "title": {"en": "My route", "mr": "माझा मार्ग", "hi": "मेरा रूट"},
    "truck": {"en": "Truck", "mr": "गाडी", "hi": "गाड़ी"},
    "evening": {"en": "Evening round", "mr": "संध्याकाळची फेरी", "hi": "शाम का राउंड"},
    "small": {"en": "(small)", "mr": "(लहान)", "hi": "(छोटी)"},
    "no_route": {"en": "No approved route yet. Please ask the supervisor.",
                 "mr": "अजून मंजूर मार्ग नाही. कृपया पर्यवेक्षकांना विचारा.",
                 "hi": "अभी कोई मंजूर रूट नहीं है। कृपया सुपरवाइज़र से पूछें।"},
    "next": {"en": "Next stop", "mr": "पुढचा थांबा", "hi": "अगला स्टॉप"},
    "planned": {"en": "Planned time", "mr": "नियोजित वेळ", "hi": "तय समय"},
    "of": {"en": "of", "mr": "पैकी", "hi": "में से"},
    "maps": {"en": "Open in Google Maps", "mr": "Google Maps मध्ये उघडा", "hi": "Google Maps में खोलें"},
    "how_full": {"en": "How full was it?", "mr": "किती भरली होती?", "hi": "कितना भरा था?"},
    "q": {"en": "¼", "mr": "¼", "hi": "¼"},
    "h": {"en": "½", "mr": "½", "hi": "½"},
    "t": {"en": "¾", "mr": "¾", "hi": "¾"},
    "f": {"en": "Full", "mr": "पूर्ण", "hi": "पूरा"},
    "o": {"en": "Overflowing", "mr": "ओसंडून वाहत", "hi": "बाहर गिर रहा"},
    "collected": {"en": "Collected", "mr": "उचलले", "hi": "उठा लिया"},
    "not_reach": {"en": "Could not reach", "mr": "पोहोचू शकलो नाही", "hi": "नहीं पहुँच पाए"},
    "missing": {"en": "Bin missing", "mr": "डबा गायब", "hi": "डिब्बा गायब"},
    "truck_full": {"en": "Truck full - go to the landfill", "mr": "गाडी भरली - डंपिंग ग्राउंडला जा",
                   "hi": "गाड़ी भर गई - लैंडफिल जाएँ"},
    "landfill": {"en": "Turbhe landfill: unload", "mr": "तुर्भे डंपिंग ग्राउंड: रिकामे करा",
                 "hi": "तुर्भे लैंडफिल: खाली करें"},
    "unloaded": {"en": "Unloaded at landfill", "mr": "रिकामे केले", "hi": "खाली कर दिया"},
    "depot": {"en": "All done - return to the depot", "mr": "सर्व पूर्ण - डेपोला परत जा",
              "hi": "सब पूरा - डिपो लौटें"},
    "remaining": {"en": "Remaining stops", "mr": "उरलेले थांबे", "hi": "बाकी स्टॉप"},
    "saved": {"en": "Saved", "mr": "जतन केले", "hi": "सेव हो गया"},
    "updated": {"en": "Route updated by the control room", "mr": "नियंत्रण कक्षाने मार्ग बदलला",
                "hi": "कंट्रोल रूम ने रूट बदला"},
    "load": {"en": "On board", "mr": "गाडीत", "hi": "गाड़ी में"},
    "who_today": {"en": "Who is on this truck today?", "mr": "आज या गाडीवर कोण आहे?", "hi": "आज इस गाड़ी पर कौन है?"},
    "who_phone": {"en": "Who is using this phone?", "mr": "हा फोन कोण वापरत आहे?", "hi": "यह फ़ोन कौन चला रहा है?"},
    "start_shift": {"en": "Start the shift", "mr": "काम सुरू करा", "hi": "काम शुरू करें"},
    "crew_today": {"en": "Crew today", "mr": "आजची टीम", "hi": "आज की टीम"},
    "change": {"en": "Change", "mr": "बदला", "hi": "बदलें"},
    "no_crew_list": {"en": "No crew list yet - ask the administrator to add the crew names in Settings.",
                     "mr": "टीमची यादी नाही - प्रशासकांना Settings मध्ये नावे जोडायला सांगा.",
                     "hi": "टीम की सूची नहीं है - एडमिन से Settings में नाम जोड़ने को कहें।"},
    "choose_fill": {"en": "Tap how full it was first", "mr": "आधी किती भरली होती ते निवडा",
                    "hi": "पहले चुनें कितना भरा था"},
}


def t(key: str, lang: str) -> str:
    return T[key].get(lang, T[key]["en"])
