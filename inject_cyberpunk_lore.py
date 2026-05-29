import sys
sys.path.insert(0, '/mnt/seagate/creature_agent')

from fiction_db import FictionDB

lore = """
CYBERPUNK RED — WORLD LORE & ATMOSPHERE
Source: Cyberpunk RED Core Rulebook / Night City sourcebooks
Setting: 2045, "The Time of the Red"

=== NIGHT CITY ===

Night City sits on the coast of California, a city-state that never sleeps and never forgives. Population roughly 5 million, though nobody's counted the bodies in the Badlands. It was built by Richard Night on corporate money in the 1990s as a utopia — he was murdered before it opened. That's Night City for you.

The city is divided into districts:
- City Center (Downtown): corpo towers, arcologies, the rich behind glass. Arasaka Tower dominates the skyline. You need credentials to breathe here.
- Watson: industrial decay, immigrant communities, cheap implants, Night Markets. Maelstrom gang territory. Where fixers find talent.
- Westbrook: Japantown and the Charter Hill money. Neon-lit pleasure district. Exotic restaurants, black market luxury, corp middle management blowing their bonuses.
- Heywood: largely Latino district, gang-controlled suburbs. Valentinos run here. Looks almost normal from the outside.
- Pacifica: abandoned resort development, poorest district, NCPD doesn't even go. The Voodoo Boys control it. Outsiders don't come back.
- Santo Domingo: factories, working poor, assembly lines. People grind here. Industrial output for the whole city.
- The Badlands: outside city walls. Nomad territories, fuel stops, lawless. If you need to disappear, head for the Badlands.

=== THE TIME OF THE RED (2045) ===

The Fourth Corporate War ended in 2023. Arasaka and Militech nuked each other's assets. The old internet — the NET — collapsed when Rache Bartmoss's DataKraash virus was released. No global internet anymore, just isolated "shards" — local networks, city-nets, corporate intranets. Netrunners work differently now: they have to physically jack into local access points, not global nodes.

What's left: a world still picking up the pieces. The sky at sunset has a permanent reddish tint from particulates — that's why they call it the Time of the Red.

Megacorporations still run everything, but they're smaller, meaner, and more paranoid after the War. They learned that open conflict is expensive. Now they do it through proxies: solos, saboteurs, deniable assets.

=== SOCIAL STRUCTURE ===

At the top: Corporate Citizens. The corpos. They live in arcologies — sealed self-contained towers. Food, medical care, entertainment, all provided. They don't interact with the street. They have Trauma Team on speed dial (private ambulance/combat medics — if your insurance lapses, they leave you bleeding).

Middle: The Edgerunners. Solos, netrunners, techies, medtechs, fixers, nomads, media. They're not corpo but they're skilled enough to survive. They live in apartments, buy their own implants, take jobs from fixers. Dangerous life. Short life, sometimes.

Below: The Proles. Factory workers, service staff, gig economy bottom-feeders. Living in coffin apartments or stacked housing. Eating vat food. One bad month from homelessness.

Street level: the gangs. Maelstrom (chrome obsessives who've replaced too much meat with machine — many are losing their minds to cyberpsychosis). Valentinos (honor-coded Latino gang in Heywood). Animals (combat drug-enhanced brawlers). 6th Street (pseudo-patriot militia in Santo Domingo). Tyger Claws (Japanese crime syndicate in Japantown).

=== CORPO LIFE ===

Corporations are the real governments. They have their own law, their own cops (Corporate Security), their own courts. If you're a corpo citizen, you technically live in a different legal jurisdiction from street people.

Corporate hierarchy is everything. Your rank determines your apartment size, your meal credits, whether you get upgraded implants or budget chrome. A mid-level corpo assistant is above the law in most ways — but she is absolutely not above her superior. Corporate culture is feudal. You serve your superior completely. Their advancement is your advancement. Their failure is your failure. Corporate women at the bottom of the ladder exist in a world of perfect professional presentation and total subservience to those above. Every humiliation is professional. Every order is obeyed. The smile never drops.

A corporate assistant in 2045: perfect posture, premium implants (muscle memory enhancers so she's never clumsy, pain dampeners so she never winces), neural interface at her temple for real-time data overlay. She sees her boss's calendar, vital signs, and mood indicators before she walks into any room. Her apartment is paid for by the corp. So is her food, her clothing allowance (within approved guidelines), her medical care. She belongs to the machine.

=== STREET CULTURE ===

On the street, style IS survival. What you wear, what chrome you're running, how you carry yourself — these signal what you are and what you'll do. Nobody respects weakness. But being obvious about your strength gets you killed. It's a performance, always.

Implants: everyone has something. Even poor people have basic neural interfaces — cyberware makes you employable. Popular implants include: kiroshi optical implants (enhanced eyes, usually glowing), subdermal plating (armor under skin), muscle grafts, pain editors, chipware slots (for inserting learned skills). Rich corpos run military-grade cyberware under tailored skin so it doesn't show. Street people wear chrome openly — it's the only luxury they have.

Food: real meat is expensive. Most people eat protein vat products, soy-based "nutri," or synth-carb packs. A real egg is a treat. A real steak means you're doing well.

Sex and relationships: the body is commodified and modified. Love hotels are everywhere. Companionship is bought and sold like everything else. Actual intimacy is rare and valuable precisely because everything else is transactional.

=== THE CORPO ASSISTANT'S DAILY LIFE ===

She wakes at 5:30. Neural alarm, no sound. Her apartment building is corporate housing — identical to all the others on floors 12 through 40. She dresses to code. Approved palette, corporate silhouette, heels that add 4cm (regulation). She eats a prepared breakfast from the corp canteen — logged, nutritionally optimized, slightly flavorless. She's in her supervisor's outer office by 7:45. She's always early. Being late means a formal note in your file. Three notes means reassignment. Reassignment means losing the apartment.

Her supervisor is five levels above her. The supervisor's supervisor is almost mythological — glimpsed at corporate events, never addressed directly.

She does work that would have been automated if the DataKraash hadn't disrupted half the AI infrastructure. She processes data requests, manages physical asset transfers, runs interference on her supervisor's schedule. She's faster than a basic AI at managing nuance, reading the room, knowing when to say what. She's paid to be invisible and flawlessly competent. She's paid to absorb her supervisor's frustration without reacting. She goes home, eats vat curry, watches pirated braindance clips of people living bigger lives, and does it again tomorrow.

=== MEGACORPORATIONS ===

ARASAKA: Japanese corporate zaibatsu. Security, weapons, banking. Practically own Japan and a third of Night City. Their corpo staff operate with a kind of samurai-code professionalism — absolute loyalty to superiors, extreme competence, complete suppression of personal needs. Their female staff are known for an almost supernatural composure.

MILITECH: American weapons and military services. Rivals to Arasaka. More aggressive and less polished.

KANG TAO: Rising Chinese arms manufacturer. Cutting edge smart weapons. Expanding fast since the War.

BIOTECHNICA: Gene engineering and biotech. They make the vat food. They also do "human optimization" contracts for corps that want physically enhanced workers.

TRAUMA TEAM: Medical response corporation. Subscribe or die. Their armored AV-4 medivacs will land in combat to extract subscribers. Won't even slow down for non-subscribers.

=== AESTHETICS ===

Neon over everything. Holographic advertisement columns 20m tall. Rain most nights (acid-tinged — rubs the chrome eventually). The gap between polished corporate space and decaying street space is violent. You can step out of a gleaming corpo lobby into a street market selling black-market implants under tarps.

Fashion: corpo-wear is severe, structured, dark. Street fashion is loud, asymmetric, chrome-accented, intentionally shocking. Gender presentation is fluid and often extreme — the body is a canvas and a statement. Subdermal LED implants are popular in club culture. Hair shaved on one side, dyed vivid colors, woven with fiber-optic threads.

Music: the Rockerboys kept the culture alive during the corporate wars. Street music is aggressive, loud, political. Corporate spaces play carefully licensed ambient electronica. Braindance (BD) is the dominant entertainment medium — literally playing someone else's recorded sensory experience including emotions. Addictive. Widely pirated.
"""

db = FictionDB('/mnt/seagate/creature_agent/fiction_db')
db.ingest_text(lore, source="Cyberpunk_RED_Lore_Compendium", chunk_size=400, overlap=80)
print("Injected Cyberpunk RED lore into fiction_db")

# Quick verify
results = db.retrieve_by_query("Night City corporate life daily routine", n=3)
print(f"\nVerify query returned {len(results)} results:")
for r in results:
    print(f"  - {r[:80]}...")
