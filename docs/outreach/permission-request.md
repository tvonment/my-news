# Asking publishers for permission (Tier A)

MyNews shows AI-written summaries only for sources that allow it (content Tier A, see the
v2 plan). Everything else gets the publisher's own headline + feed teaser and a link. These
templates ask for that permission. Every answer, yes or no, is legal evidence: record it on the
source's catalog entry (from Phase 2: `mynews_admin.py catalog set-tier`) with the date and a
copy of the reply.

Send from the operator address that the privacy policy will name. Find each recipient on the
publisher's own contact or licensing page. Do not guess addresses.

| Source | Ask | Where to find the contact |
|---|---|---|
| SRG SSR (SRF; RTS later) | Summaries **and** translation (de/en), because the published allowance covers "RAG/grounding with attribution", not summaries | partnermanagement@srgssr.ch |
| Six Colors, MacStories, Daring Fireball, The Eclectic Light Company, TidBITS | Summaries, translation optional | Contact page of each site |
| Mac & i (heise), golem | Summaries; note that heise also carries dpa content, which needs dpa's consent | Licensing / Lizenzen page |
| ECB, Federal Reserve | Confirm that the published reuse terms cover AI summaries with attribution | Their copyright / reuse pages |

## German (SRG, heise, golem)

> **Betreff:** Erlaubnis für KI-Kurzzusammenfassungen mit Link auf Ihre Artikel (MyNews, privates Beta)
>
> Guten Tag
>
> Ich entwickle MyNews, eine kleine, nicht kommerzielle News-App für iOS. Sie läuft vorerst als
> geschlossene Beta für höchstens 50 eingeladene Personen in der Schweiz. MyNews liest öffentliche
> RSS-Feeds, gruppiert Meldungen zum selben Ereignis und zeigt jeder Person eine kurze, endliche
> Auswahl an. Jede Meldung verlinkt prominent auf den Originalartikel bei Ihnen.
>
> Ohne Ihre Erlaubnis zeigt MyNews von [PUBLIKATION] nur Ihren Titel, Ihren Feed-Teaser und den Link,
> und Ihre Texte gehen über den Titel hinaus an kein KI-Modell. Ich frage an, ob ich zusätzlich Folgendes
> tun darf:
>
> 1. aus dem Volltext Ihres Feeds oder Artikels eine Zusammenfassung aus drei kurzen Punkten (je ein Satz) erstellen,
>    deutlich gekennzeichnet als «Zusammenfassung durch KI», mit Quelle und Link;
> 2. diese Zusammenfassung zusätzlich auf Englisch anzeigen (für eingeladene Personen, die Englisch
>    eingestellt haben);
> 3. die dafür nötigen Abrufe unter Einhaltung Ihrer robots.txt vornehmen.
>
> Was ich nicht tue: Ihre Inhalte für das Training von KI-Modellen verwenden, Paywalls umgehen,
> Artikel länger als 21 Tage speichern oder Ihre Texte ohne Link weitergeben. Die Verarbeitung läuft
> in Microsoft Azure (Schweiz Nord, EU-Datenzone). Sie können eine Erlaubnis jederzeit per E-Mail
> widerrufen; die Zusammenfassungen werden dann sofort entfernt.
>
> Gerne beantworte ich Fragen oder passe den Umfang an Ihre Bedingungen an.
>
> Freundliche Grüsse
> [NAME]
> [KONTAKT / SUPPORT-SEITE]

## English (indie and tech sources, ECB, Fed)

> **Subject:** Permission for short, linked AI summaries of your articles (MyNews, private beta)
>
> Hello,
>
> I'm building MyNews, a small non-commercial iOS news reader. For now it's a closed beta for at most
> 50 invited people in Switzerland. It reads public RSS feeds, groups reports about the same event,
> and gives each person a short, finite brief. Every item links prominently to your original article.
>
> Without your permission, MyNews shows only your headline, your feed teaser and the link for
> [PUBLICATION], and none of your text beyond the title reaches an AI model. I'd like to ask whether I
> may also:
>
> 1. generate a summary of three short points (one sentence each) from your feed's full text, clearly labelled "Summary by AI",
>    always with your name and a link;
> 2. show that summary in German as well, for invited readers who use German;
> 3. fetch what that needs while honouring your robots.txt.
>
> What I won't do: train AI models on your content, get around paywalls, keep articles longer than
> 21 days, or show your text without a link back. Processing runs in Microsoft Azure (Switzerland
> North, EU Data Zone). You can withdraw permission at any time by email, and the summaries are
> removed straight away.
>
> Happy to answer questions or adapt the scope to your terms.
>
> Best regards,
> [NAME]
> [CONTACT / SUPPORT PAGE]
