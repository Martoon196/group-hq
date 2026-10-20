# Privacy notice additions

Written 20 October 2026. The paragraphs to add to the four privacy notices before any landlord data
moves between MH196 2026 Ltd and Hello New Home 2 Ltd. Each block names the other company as the
recipient and states the purpose, matches `data-sharing-agreement.md` and `consent-texts.md`, and is
written in the voice of the notice it goes into. [CONFIRM] marks what the solicitor should settle.
Group HQ needs no public notice: it is internal and holds emails and ids only.

## propertypredator.com (`frontend/privacy.html`, controller MH196 2026 Ltd)

**Section 1, What we collect.** Add a row to the table:

| Data | Where it comes from |
|---|---|
| The name, email address and phone number of a landlord, and the address, postcode and letting details of a property, when you use "Let this property with Hello New Home" | You, typed on the pipeline deal or deal pack page after the landlord has agreed to be contacted |

**Section 2, Why we use it.** Add two rows:

| Purpose | Lawful basis |
|---|---|
| Passing a property and its landlord's contact details to Hello New Home at your request | The landlord's consent, which you confirm when you send it |
| Telling Hello New Home who introduced the property (your account email and brand contact name) and showing you how the introduction is going | Legitimate interests (tracking the introduction you asked for) |

**Section 3, Who we share it with.** Add after the processor table:

> **Hello New Home.** If you ask us to, we pass a property and its landlord's contact details to Hello
> New Home 2 Ltd (company number 13752357), the lettings business in our group, so that it can
> contact the landlord about letting the property. Hello New Home is a data controller in its own
> right for what it receives, and its privacy notice at hellonewhome.co.uk/privacy applies. We send
> only what you type on the page, the property details on the deal, and your own account email and
> brand contact name as the introducer. Hello New Home tells us only how the introduction is going
> (its reference and status) and does not use your details for marketing. Nothing is sent unless you
> press the button, and you must have the landlord's agreement first.

**Section 5, How long we keep it.** Add:

> Hello New Home handoffs: the record of what we sent, including the landlord's name, for 12 months
> after the last change of status, or for as long as the deal if Hello New Home is instructed. [CONFIRM]

**Section 6, "If you are not a customer".** Add:

> **If a Property Predator member introduced your property to Hello New Home.** The member confirmed
> that you had agreed to be contacted. If that is not right, email us or Hello New Home
> (lettings@hellonewhome.co.uk) and the introduction is closed and deleted.

## keypropertiesuk.co.uk (public site notice; the deal portal has no privacy page of its own and should link to this one [CONFIRM where the notice lives])

Add a section headed **Hello New Home**:

> **If you source deals for us.** When you or Key Properties UK send a deal to Hello New Home ("Let
> with Hello New Home" on the deal page), your name and portal email go with it as the person who
> introduced the property, so that Hello New Home 2 Ltd can tell you how the introduction is going and
> credit it to you. This is in our legitimate interests and yours. Hello New Home does not use your
> details for anything else.

> **If you buy a property through us and will let it.** Once you have agreed to be contacted about
> letting it, your name, email address and phone number, with the property's address, postcode,
> bedrooms and expected rent, are sent to Hello New Home 2 Ltd (company number 13752357), our sister
> lettings business, which will contact you to offer a free rent review and, if you instruct it, let
> and manage the property. Hello New Home is the data controller for what it receives; its notice is at
> hellonewhome.co.uk/privacy. We send your details only after you have agreed, and we record when you
> agreed and the wording you agreed to. Hello New Home tells us only the reference and status of the
> introduction. If instead you ask Hello New Home yourself through the link in an investor pack, we send
> nothing; Hello New Home tells us that you asked and how it went. [CONFIRM]

> **What we keep.** The deal keeps Hello New Home's reference and status. It never holds your
> contact details for this purpose; those live with Hello New Home.

## hellonewhome.co.uk (`app/templates/public/pages/privacy.html`, controller Hello New Home 2 Limited)

The notice is still a draft with bracketed placeholders (date, ICO number, retention periods); fill
those in the same revision [CONFIRM].

**What we collect.** Add a bullet:

> **Landlords introduced to us:** your name, contact details, property address and letting details,
> the time you agreed to be contacted and the wording you agreed to, received from Property Predator or
> Key Properties UK (both run by MH196 2026 Ltd, a company in our group) when the person dealing with
> your property asked us to contact you and confirmed that you had agreed; and that person's name and
> email as the introducer.

**Why we use it.** Add:

> With your consent, to contact you about letting a property that was introduced to us, and from the
> moment you instruct us, to perform the contract with you. Tell us to stop at any time and we will
> close the introduction and delete it.

**Who we share it with.** Add:

> MH196 2026 Ltd, which runs Property Predator, Key Properties UK and Ordris. We tell the business that
> introduced your property our reference and the status of the introduction, never your contact details
> beyond what it sent us. Once you instruct us, we keep your landlord, property and tenancy records in
> Ordris, the property management software run by MH196 2026 Ltd, which processes them on our
> instructions.

**How long we keep it.** Add:

> An introduction that does not lead to an instruction is deleted 12 months after we last dealt with
> it. If you instruct us, the records are kept for six years after the relationship ends, as above.

## ordris.io (`artifacts/ordris/src/pages/privacy.tsx`, controller MH196 2026 Ltd)

**Section 2, Data we collect.** Add **2.4 Landlords introduced by Hello New Home**:

> Hello New Home 2 Ltd (company number 13752357), a company in our group, uses Ordris to manage the
> properties it is instructed on. When Hello New Home is instructed, it enters the landlord's name,
> email address and phone number, the property's address and details and, later, the tenancy into its
> own Ordris organisation through our partner interface. Hello New Home is the data controller for those
> records and we process them on its instructions, as we do for every organisation's data on the
> platform; Hello New Home's privacy notice at hellonewhome.co.uk/privacy applies.

**Section 4, Who we share data with.** Replace the "Group companies" bullet:

> **Group companies.** Hello New Home 2 Ltd holds its landlord, property and tenancy records in its own
> Ordris organisation, as described in section 2.4. For our own accounts we record that an instruction
> was received from Hello New Home: a reference number and record ids, with no personal data. We may
> also share data with Group Companies (as defined in our Deed of Guarantee) for the purposes of
> restructuring or novation of the Rent Guarantee product.

The "Cloud infrastructure" bullet in the same section still names Google Cloud. Once Ordris runs on
Render (Frankfurt) with its database on Neon (London), change it to say so [CONFIRM timing with the
cutover].

**Section 6, How long we keep your data.** Add a row:

| Category | Retention period |
|---|---|
| Records entered by Hello New Home | For as long as Hello New Home keeps them (its notice says six years after the relationship ends) |

## Publishing order

1. Sign the data sharing agreement.
2. Publish the Hello New Home paragraphs (it receives the data) and the Property Predator and Key
   Properties paragraphs (they send it) in the same week; update each notice's "last updated" date.
3. Publish the Ordris paragraphs before the first instruction is sent to Ordris (Phase 3 C3).
4. Only then take Hello New Home's API clients out of dry run.
