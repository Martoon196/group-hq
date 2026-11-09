"""Local first-pass intent classifier. Predictions never change CRM membership or authorise a reply."""
import math
import re
from collections import Counter

# Short labelled examples train a multinomial text classifier. Sender direction matters:
# Martin's own offer is context for a response, never evidence that the other person is a seller.
EXAMPLES = {
    "personal": [
        "happy birthday mate hope you have a lovely day", "are you coming for dinner tonight",
        "do you fancy a pint at the pub", "see you at football on Saturday mate",
        "how are the kids and the family", "mum said she will pick the kids up from school",
        "let's meet for coffee and catch up", "what time are we having lunch tomorrow",
        "thanks for coming to the wedding love you", "are you watching the match tonight",
    ],
    "pitch": [
        "we help businesses like yours generate leads are you open to a demo",
        "i offer website development seo and social media management for your business",
        "i would love to book you in for a discovery call about our services",
        "would you like us to handle your marketing we have a special offer",
        "we can get you more clients with our lead generation services",
        "can i send you our pricing for my agency", "would you be interested in trying our software",
        "my team can build you a new website", "we offer virtual assistants for busy business owners",
    ],
    "business": [
        "i am looking to buy an investment property in durham",
        "can you send the details for that house and arrange a viewing",
        "i am investing in buy to lets around the north east",
        "i need a letting agent to manage my rental property",
        "i am looking for bridging finance on a development project",
        "how do i join your mentoring course and what does it cost",
        "can i get the webinar link and the action plan",
        "i am selling my house what would you offer",
        "do you have any deals that fit my budget",
        "what is the address and asking price of the property",
        "my tenants need help with repairs who do i contact",
        "where do i sign up for the training and calculators",
        "looking for a mortgage on a development site",
    ],
}
# Route by service, allowing several brands when one person has several needs.
SERVICES = (
    ("hnh", "Lettings or property management", r"\b(?:letting(?:s| agent)?|tenan(?:t|ts|cy)|manage my (?:property|rental)|property management)\b"),
    ("piuk", "Finance or development lending", r"\b(?:bridging|(?:development|property|project) (?:loan|lending|funding|finance)|mortgage|refinanc\w*)\b"),
    ("mhp", "Training or mentoring", r"\b(?:course|webinar|mentor\w*|training|deal desk|first door|income path|action plan)\b"),
    ("kp", "Property listing or viewing", r"\b(?:durham|asking price|viewing|key properties|keypropertiesuk|listing)\b"),
    ("pp", "Buying, selling or sourcing a deal", r"\b(?:property predator|sell(?:ing)? my (?:house|property)|sourc(?:e|ing) (?:a |property )?deal|property sourc\w*|off.market deal)\b"),
    ("ordris", "Ordris enquiry", r"\bordris\b"),
)
HUMAN = re.compile(r"\b(?:complain\w*|refund|solicitor|court|formal notice|lawyer|fraud|payment details|bank details|transfer money|urgent|emergency|stop|unsubscribe|do not contact|don't contact|speak to (?:martin|a person|a human))\b", re.I)
def tokens(text):
    return re.findall(r"[a-z]{2,}", text.casefold())

COUNTS = {kind: Counter(t for sentence in texts for t in tokens(sentence)) for kind, texts in EXAMPLES.items()}
VOCAB = set().union(*COUNTS.values())
TOTALS = {k: sum(v.values()) + len(VOCAB) for k, v in COUNTS.items()}


def analyse(messages, account_brand=None):
    """Score recent inbound text only. A weak match or a sensitive request goes to the owner."""
    incoming = [m for m in messages if m.get("direction") == "incoming" and str(m.get("body") or "").strip()]
    evidence = " ".join(str(m["body"])[:1000] for m in incoming[-3:])
    latest = str(incoming[-1]["body"])[:1800] if incoming else ""
    words = [t for t in tokens(evidence) if t in VOCAB]
    scores = sorted(((sum(math.log((counts[t]+1)/TOTALS[kind]) for t in words), kind)
                     for kind, counts in COUNTS.items()), reverse=True)
    certain = len(set(words)) >= 3 and scores[0][0] - scores[1][0] >= 1.0
    kind = scores[0][1] if certain else "unclassified"
    routes = [(brand, intent) for brand, intent, pattern in SERVICES if re.search(pattern, evidence, re.I)]
    if kind == "unclassified" and routes and re.search(r"\b(?:need|looking for|looking to|interested in|would like|can you|could you|how do i)\b", latest, re.I):
        kind = "business"
    brands = [b for b, _ in routes] if kind == "business" else []
    if kind == "business" and not brands and account_brand:
        brands = [account_brand]
    handoff = bool(HUMAN.search(latest)) or kind == "unclassified" or (kind == "business" and not brands)
    intent = ", ".join(i for _, i in routes) if kind == "business" and routes else {
        "unclassified": "More context needed", "business": "Business enquiry", "personal": "Personal conversation", "pitch": "Offering a product or service"}[kind]
    return {"kind": kind, "brands": brands, "intent": intent, "needs_owner": handoff,
            "reason": "Owner attention requested" if HUMAN.search(latest) else "Recent incoming message",
            "draft": "", "source": "Local classifier"}
