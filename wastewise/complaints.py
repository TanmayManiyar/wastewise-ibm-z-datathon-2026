"""Understanding citizen complaints written in plain words.

Works on English, Hindi, Marathi and mixed text (Hinglish, Devanagari +
Latin). Two things are extracted:
  * the problem: overflowing, full, fire, dead animal, not collected,
    damaged, smell (several may apply),
  * the place: candidate bins, scored by matching the words of the complaint
    against each bin's landmark, street, site type and number. Rare words
    count more (inverse document frequency), spelling mistakes are tolerated
    (fuzzy matching), and Hindi/Marathi place words are mapped to their
    English forms or transliterated from Devanagari.
The parser never decides alone: the UI shows "We understood: ... - confirm?",
never pre-selects a bin, and asks the user to choose when several places match.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

PROBLEMS = {
    "fire": ["fire", "burning", "burnt", "smoke", "aag", "jal raha", "jal rahi", "dhuan", "dhur",
             "आग", "जल रहा", "जल रही", "धुआं", "धुआँ", "धूर", "आग लागली", "जळत"],
    "dead_animal": ["dead dog", "dead cat", "dead animal", "dead rat", "carcass", "dead body of", "mara hua",
                    "mari hui", "मरा हुआ", "मरी हुई", "मरा कुत्ता", "मृत", "मेलेला", "मेलेले", "मेलेली", "dead"],
    "overflowing": ["overflow", "overflowing", "spilling", "spill", "spilled", "on the road", "lying on road",
                    "falling out", "bahar gir", "road pe", "road par", "sadak par", "बाहर गिर", "सड़क पर", "सडक पर",
                    "रस्त्यावर", "वाहत", "ओसंडून", "भरून वाहत", "फैल", "पसरला", "पडला", "पडले", "पडलेला", "पड़ा", "बिखरा",
                    "padla", "pada hai", "bikhra", "pasarla"],
    "full": ["full", "filled", "bhar gaya", "bhara", "bharla", "bharli", "भर गया", "भरा", "भरी", "भरले", "भरलेली",
             "भरला", "पूर्ण भर"],
    "not_collected": ["not collected", "not picked", "not emptied", "not cleared", "no one came", "nobody came",
                      "truck did not come", "nahi uthaya", "nahi utha", "uthaya nahi", "uchalla nahi", "nahi aali",
                      "उठाया नहीं", "नहीं उठाया", "नहीं उठा", "उचलला नाही", "उचलले नाही", "गाडी आली नाही",
                      "गाड़ी नहीं आई", "कोणी आले नाही"],
    "damaged": ["broken", "damaged", "crack", "lid", "tuta", "toota", "phata", "टूटा", "टूटी", "फटा",
                "तुटलेली", "तुटला", "तुटले", "मोडलेली"],
    "smell": ["smell", "stink", "stench", "odour", "odor", "badbu", "badboo", "vaas", "बदबू", "दुर्गंध",
              "वास", "घाण वास"],
}
PROBLEM_WORDS = {
    "overflowing": "overflowing", "full": "full", "fire": "fire", "dead_animal": "dead animal",
    "not_collected": "not collected", "damaged": "damaged bin", "smell": "bad smell",
}
URGENT = {"fire", "dead_animal"}

# Hindi / Marathi / Hinglish place words -> English alias words used in the bin register.
PLACE_WORDS = {
    "सानपाडा": "sanpada", "सानपाड़ा": "sanpada", "जुईनगर": "juinagar", "जुई": "jui", "तुर्भे": "turbhe",
    "वाशी": "vashi", "स्टेशन": "station", "रेल्वे": "railway", "रेलवे": "railway", "स्थानक": "station",
    "बस": "bus", "डिपो": "depot", "स्टॉप": "stand", "डीमार्ट": "dmart", "डी-मार्ट": "dmart", "मार्ट": "mart",
    "शाळा": "school", "स्कूल": "school", "विद्यालय": "school", "महाविद्यालय": "college", "कॉलेज": "college",
    "रुग्णालय": "hospital", "हॉस्पिटल": "hospital", "अस्पताल": "hospital", "दवाखाना": "clinic", "क्लिनिक": "clinic",
    "मंदिर": "mandir", "देऊळ": "mandir", "गणपती": "ganesh", "गणेश": "ganesh", "दत्त": "datta", "चर्च": "church",
    "गुरुद्वारा": "gurudwara", "उद्यान": "park", "बगीचा": "park", "बाग": "park", "पार्क": "park",
    "मैदान": "ground", "ग्राउंड": "ground", "सोसायटी": "society", "सोसाइटी": "society", "बिल्डिंग": "tower",
    "टॉवर": "tower", "गाव": "village", "गांव": "village", "गाँव": "village", "पोलीस": "police", "पुलिस": "police",
    "ठाणे": "police", "सेक्टर": "sector", "मार्केट": "market", "बाजार": "market", "मंडई": "market",
    "रस्ता": "road", "मार्ग": "marg", "रोड": "road", "चौक": "chowk",
    "dmart": "dmart", "d-mart": "dmart", "d mart": "dmart", "mandir": "mandir", "temple": "mandir",
    "masjid": "masjid", "gaon": "village", "gav": "village", "rly": "railway", "stn": "station",
    "hospital": "hospital", "hopital": "hospital",
}
# Site type words that should also match a bin of that type.
SITE_WORDS = {
    "station": "station", "railway": "station", "bus": "bus_stand", "stand": "bus_stand", "depot": "bus_stand",
    "school": "school", "college": "college", "hospital": "hospital", "clinic": "clinic", "mandir": "temple",
    "church": "worship", "gurudwara": "worship", "park": "park", "ground": "ground", "society": "housing",
    "chs": "housing", "apartment": "housing", "tower": "housing", "village": "gaothan", "police": "public_office",
    "market": "supermarket", "dmart": "supermarket", "supermarket": "supermarket", "restaurant": "eatery",
    "hotel": "eatery",
}
STOP = {"the", "a", "an", "of", "near", "at", "in", "on", "is", "are", "and", "to", "from", "since", "for", "please",
        "pls", "plz", "very", "bin", "dustbin", "garbage", "waste", "kachra", "kachara", "kooda", "kuda", "ke", "ki",
        "ka", "paas", "pas", "hai", "he", "ho", "jawal", "javal", "javal", "che", "chi", "cha", "la", "aahe", "ahe",
        "sir", "madam", "our", "my", "there", "this", "that", "it", "its", "has", "have", "been", "not", "no",
        "road", "marg", "lane", "sector", "next", "behind", "opposite", "front", "outside", "full", "overflowing",
        "smell", "bad", "lot", "lying", "near", "by", "with", "very", "too", "much", "all", "today", "morning",
        "evening", "yesterday", "days", "day", "hours", "since", "still", "again", "here",
        "के", "की", "का", "पास", "है", "हैं", "में", "से", "पर", "को", "जवळ", "आहे", "च्या", "ची", "चा", "ला", "मध्ये",
        "कृपया", "कचरा", "कूड़ा", "कूड़ेदान", "कूडा", "कचराकुंडी", "डस्टबिन", "बहुत", "खूप", "आणि", "और",
        "please", "dabba", "kunda", "kundi"}

_DEV_VOWELS = {"अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
               "ऋ": "ri", "ॲ": "a", "ऑ": "o"}
_DEV_SIGNS = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
              "ृ": "ri", "ॅ": "e", "ॉ": "o", "ं": "n", "ँ": "n", "ः": "h", "्": "", "़": ""}
_DEV_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
             "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh",
             "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "ळ": "l",
             "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "क्ष": "ksh", "ज्ञ": "gy", "ड़": "d", "ढ़": "dh",
             "फ़": "f", "ज़": "z"}


def transliterate(word: str) -> str:
    """Rough Devanagari -> Latin (enough to match proper nouns like रायन -> ryan)."""
    out = []
    chars = list(unicodedata.normalize("NFC", word))
    for i, ch in enumerate(chars):
        if ch in _DEV_CONS:
            out.append(_DEV_CONS[ch])
            nxt = chars[i + 1] if i + 1 < len(chars) else ""
            if nxt not in _DEV_SIGNS and nxt and nxt in _DEV_CONS:
                out.append("a")       # inherent vowel between consonants
        elif ch in _DEV_VOWELS:
            out.append(_DEV_VOWELS[ch])
        elif ch in _DEV_SIGNS:
            out.append(_DEV_SIGNS[ch])
        elif ch.isascii():
            out.append(ch)
    return "".join(out).replace("aa", "a")


def is_devanagari(s: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" for ch in s)


def detect_language(text: str) -> str:
    dev = sum("ऀ" <= ch <= "ॿ" for ch in text)
    lat = sum(ch.isascii() and ch.isalpha() for ch in text)
    marathi = len(re.findall(r"आहे|जवळ|नाही|कचराकुंडी|भरून|वाहत|च्या|मध्ये|आणि|झाला|झाली|उचल|रस्त्यावर|खूप|तुटले|गाडी आली", text))
    hindi = len(re.findall(r"है|के पास|नहीं|कूड़ा|कूड़े|बहुत|और|गया|रहा|सड़क|उठाया|में|बदबू", text))
    if dev and dev >= lat:
        if lat > 6:
            return "mixed"
        return "mr" if marathi > hindi else "hi"
    if dev:
        return "mixed"
    hinglish = len(re.findall(r"\b(hai|ke paas|nahi|kachra|kachara|bhar gaya|jawal|aahe|bharla|uchla|badbu|road pe|"
                              r"gaya|raha|kooda|kuda)\b", text.lower()))
    return "mixed" if hinglish else "en"


def tokens(text: str) -> list[str]:
    t = unicodedata.normalize("NFC", text.lower())
    for k in sorted(PLACE_WORDS, key=len, reverse=True):
        if " " in k or "-" in k:
            t = t.replace(k, PLACE_WORDS[k])
    raw = re.findall(r"[ऀ-ॿ]+|[a-z0-9]+", t)
    out = []
    for w in raw:
        if w in STOP:
            continue
        if w in PLACE_WORDS:
            out.append(PLACE_WORDS[w])
        elif is_devanagari(w):
            out.append(transliterate(w))
        else:
            out.append(w)
    return [w for w in out if len(w) >= 2 and w not in STOP]


def detect_problems(text: str) -> list[str]:
    t = " " + unicodedata.normalize("NFC", text.lower()) + " "
    found = []
    for prob, words in PROBLEMS.items():
        for w in words:
            if (re.search(r"\b" + re.escape(w) + r"\b", t) if w.isascii() else w in t):
                found.append(prob)
                break
    if "overflowing" in found and "full" in found:
        found.remove("full")
    if "dead_animal" in found and "fire" in found and "dead" not in t:
        found.remove("dead_animal")
    order = ["fire", "dead_animal", "overflowing", "full", "not_collected", "damaged", "smell"]
    return sorted(found, key=order.index)


@dataclass
class Candidate:
    bin_id: str
    landmark: str
    street: str
    score: float
    matched: list[str] = field(default_factory=list)


class PlaceIndex:
    def __init__(self, bins: list[dict]):
        self.bins = bins
        # word -> weight; words only found in the street name count less than the landmark
        self.alias: dict[str, dict[str, float]] = {}
        self.bigrams: dict[str, set[tuple[str, str]]] = {}
        df: dict[str, int] = {}
        for b in bins:
            words = {w: 0.6 for w in tokens(b["street"] or "")}
            words.update({w: 1.0 for w in tokens(b["landmark"])})
            words.update({b["id"].lower(): 1.0, (b["id"].split("-")[-1].lstrip("0") or "0"): 1.0,
                          b["site_type"]: 1.0})
            for w, st in SITE_WORDS.items():
                if st == b["site_type"]:
                    words[w] = 1.0
            self.alias[b["id"]] = words
            lt = tokens(b["landmark"])
            self.bigrams[b["id"]] = set(zip(lt, lt[1:]))
            for w in words:
                df[w] = df.get(w, 0) + 1
        n = len(bins)
        self.idf = {w: math.log((n + 1) / (c + 0.5)) for w, c in df.items()}

    @staticmethod
    def _sim(a: str, b: str) -> float:
        if a == b:
            return 1.0
        if len(a) < 4 or len(b) < 4:
            return 0.0
        if a.startswith(b) or b.startswith(a):
            return 0.9 if min(len(a), len(b)) >= 5 else 0.0
        r = SequenceMatcher(None, a, b).ratio()
        return r if r >= 0.8 else 0.0

    def search(self, text: str, top: int = 5) -> list[Candidate]:
        q = tokens(text)
        # explicit bin numbers: "SNP-014", "bin 14", "14 number"
        nums = set(re.findall(r"snp[-\s]?0*(\d{1,3})", text.lower()))
        nums |= set(re.findall(r"(?:bin|dabba|no\.?|number|नंबर|क्रमांक)\s*0*(\d{1,3})", text.lower()))
        out = []
        for b in self.bins:
            words = self.alias[b["id"]]
            score, matched = 0.0, []
            for w in q:
                best, bw = 0.0, None
                for a, wt in words.items():
                    s = self._sim(w, a) * wt
                    if s > best:
                        best, bw = s, a
                if bw:
                    score += best * self.idf.get(bw, 1.0)
                    matched.append(bw)
            # words in the same order as in the landmark name ("sanpada railway") count extra
            qb = set(zip(q, q[1:]))
            score += 2.0 * len(qb & self.bigrams[b["id"]])
            if b["id"].split("-")[-1].lstrip("0") in nums:
                score += 10.0
                matched.append(b["id"])
            if score > 0:
                out.append(Candidate(b["id"], b["landmark"], b["street"] or "", round(score, 2), matched))
        out.sort(key=lambda c: -c.score)
        return out[:top]


@dataclass
class Understanding:
    language: str
    problems: list[str]
    candidates: list[Candidate]
    decision: str            # "one" | "choose" | "none"
    urgent: bool

    def summary(self) -> str:
        probs = ", ".join(PROBLEM_WORDS[p] for p in self.problems) or "a problem"
        if self.decision == "one":
            c = self.candidates[0]
            return f"{probs} at {c.bin_id} {c.landmark} ({c.street})"
        if self.decision == "choose":
            return f"{probs} - several places match, please choose"
        return f"{probs} - place not recognised, please choose the bin"

    def to_json(self) -> dict:
        return {"language": self.language, "problems": self.problems, "decision": self.decision,
                "urgent": self.urgent,
                "candidates": [{"bin_id": c.bin_id, "landmark": c.landmark, "score": c.score, "matched": c.matched}
                               for c in self.candidates]}


def understand(text: str, index: PlaceIndex, min_score: float = 1.5, margin: float = 1.35) -> Understanding:
    cands = [c for c in index.search(text) if c.score >= min_score]
    if not cands:
        decision = "none"
    elif len(cands) == 1 or cands[0].score >= margin * cands[1].score:
        decision = "one"
        cands = cands[:1] + [c for c in cands[1:] if c.score >= cands[0].score / margin]
    else:
        decision = "choose"
        cands = [c for c in cands if c.score >= cands[0].score / margin][:5]
    probs = detect_problems(text)
    return Understanding(detect_language(text), probs, cands, decision, bool(set(probs) & URGENT))


# ------------------------------------------------------------------ storage
FILL_FOR_PROBLEM = {"overflowing": 100.0, "full": 100.0, "not_collected": 100.0}


def record(conn, text: str, bin_id: str | None, und: Understanding, user: str | None,
           reporter: str | None = None, synthetic: bool = False, status: str = "open") -> int:
    """Store a complaint. status "open": the place was confirmed by a user, and a full or
    overflowing report becomes a reading. "new": arrived by API, place still to be confirmed."""
    import json

    from . import audit, db
    problem = und.problems[0] if und.problems else "other"
    with db.tx(conn):
        cur = conn.execute("INSERT INTO complaints(ts, text, lang, bin_id, problem, status, reporter, created_by,"
                           " parsed, synthetic) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (db.now(), text, und.language, bin_id, problem, status, reporter, user,
                            json.dumps(und.to_json()), int(synthetic)))
        cid = int(cur.lastrowid)
        if status == "open" and bin_id and any(p in FILL_FOR_PROBLEM for p in und.problems):
            db.add_reading(conn, bin_id, 100.0, "complaint", user=user, note=f"complaint #{cid}",
                           synthetic=synthetic)
        audit.log(conn, user, "complaint_recorded", {"complaint_id": cid, "bin_id": bin_id, "problem": problem,
                                                     "language": und.language})
        db.bump_version(conn)
    return cid


def resolve(conn, cid: int, user: str, note: str | None = None) -> None:
    from . import audit, db
    with db.tx(conn):
        conn.execute("UPDATE complaints SET status='resolved', resolved_by=?, resolved_at=? WHERE id=?",
                     (user, db.now(), cid))
        audit.log(conn, user, "complaint_resolved", {"complaint_id": cid, "note": note})
        db.bump_version(conn)


def move(conn, cid: int, new_bin: str, user: str) -> None:
    """Attach a report to the right bin; the originally chosen bin is kept."""
    from . import audit, db
    r = db.one(conn, "SELECT bin_id, original_bin_id FROM complaints WHERE id=?", (cid,))
    with db.tx(conn):
        conn.execute("UPDATE complaints SET original_bin_id=COALESCE(original_bin_id, ?), bin_id=? WHERE id=?",
                     (r["bin_id"], new_bin, cid))
        audit.log(conn, user, "complaint_moved", {"complaint_id": cid, "from": r["bin_id"], "to": new_bin})
        db.bump_version(conn)


def confirm(conn, cid: int, bin_id: str, user: str) -> None:
    """A user confirms the place of a complaint that arrived by API ("new" -> "open")."""
    import json

    from . import audit, db
    r = db.one(conn, "SELECT * FROM complaints WHERE id=?", (cid,))
    parsed = json.loads(r["parsed"] or "{}")
    with db.tx(conn):
        conn.execute("UPDATE complaints SET status='open', bin_id=? WHERE id=?", (bin_id, cid))
        if any(p in FILL_FOR_PROBLEM for p in parsed.get("problems", [])):
            db.add_reading(conn, bin_id, 100.0, "complaint", user=user, note=f"complaint #{cid}",
                           synthetic=bool(r["synthetic"]))
        audit.log(conn, user, "complaint_confirmed", {"complaint_id": cid, "bin_id": bin_id})
        db.bump_version(conn)
