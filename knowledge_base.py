"""
Base de connaissances Max Sauveur - Extrait des historiques SAV
Ce fichier est utilisé comme contexte supplémentaire pour Claude lors de la génération des réponses.
"""

KNOWLEDGE_BASE = """
=== BASE DE CONNAISSANCES SAV MAX SAUVEUR ===

## IDENTITÉ
- Marque : Max Sauveur
- Produits : chaussures en cuir (mocassins, boots, derbies, brogues, ceintures)
- Service client signé : John – Service Client – Max Sauveur
- Ton : friendly, professionnel, naturel, humain. Jamais pompeux.
- Tutoyement uniquement si le client tutoie en premier.

---

## POINTURES & MODÈLES

### Méthode de mesure
- Mesurer le pied nu, talon contre un mur, marquer le bout du gros orteil
- Ajouter 0,2 cm à la mesure obtenue
- Ne jamais se baser sur ses pointures de sneakers (les sneakers taillent plus grand)

### Correspondances clés par modèle
- Dakota (Chelsea Boots) : chausse légèrement GRAND → prendre sa taille habituelle ou descendre d'une demi-taille
- Paul (Mocassin) : chausse JUSTE au début → cuir + semelle liège s'assouplissent avec le port
- Deck (Bateau) : chausse légèrement GRAND
- Brogue Derby : chausse légèrement GRAND
- Wayne / Powell : forme similaire, même pointure
- Logger Boots : pointure habituelle
- Winston : chausse LARGE → prendre une demi-taille en dessous
- Springsteen : chausse légèrement grand
- Chapman (Chelsea) : précommande, chausse ajusté

### Conseils pointure
- Mocassin : pas de lacets → maintien dépend du cou-de-pied. Si cou-de-pied fort, prendre la taille au-dessus
- Semelle en liège : se moule au pied, gagne en confort après quelques ports
- Cuir épais : rigide au début, s'assouplit rapidement
- Pour pieds larges : ne pas descendre en dessous de la bonne pointure

---

## PROCÉDURES SAV

### Retours & Échanges
- Retour par le CLIENT à ses frais (sauf défaut de fabrication ou erreur de préparation)
- Pas d'étiquette de retour fournie sauf erreur de notre part
- Bon de retour disponible : https://maxsauveur.com/pages/livraison-et-retour
- Indiquer sur le bon : raison du retour ("demande d'avoir", "défaut fabrication", etc.)
- Délai légal remboursement : 14 jours après réception. Au-delà → avoir ou échange uniquement
- Échanges directs impossibles → système d'avoir uniquement
- Avoir = code promo valable 6 mois, usage unique, non cumulable, sur le même produit
- Pour les ceintures : avoir = même produit même couleur (sauf ajustement stock)
- Commande incomplète retournée : impossible de rembourser tant que tout n'est pas reçu

### Réparation
- Procédure : client retourne la paire avec bon de retour (mention "défaut fabrication" ou "réparation")
- Adresse logistique : SSL – Solutions & Services Logistiques, 14 avenue Lamartine, 13170 Les Pennes-Mirabeau
- Délai atelier : 6 à 8 semaines
- Ressemelage Vibram : 55€ (demande spécifique hors standard)
- Semelle garantie à vie = ressemelable (pas gratuit, mais toujours possible)

### Défauts
- Si défaut avéré : on prend en charge le retour ET la réparation/échange
- Demander une photo avant toute décision
- Lacets = élément d'usure, pas un défaut de fabrication
- Ne jamais proposer des lacets gratuits sans y avoir été invité

---

## LIVRAISON & EXPÉDITION

### Transporteurs
- Chrono2Shop : livraison en point relais Chronopost
- Colissimo : livraison à domicile ou point relais
- DPD, Chronopost Express disponibles
- Pour l'international : 10-15 jours ouvrés selon transporteur

### Changement d'adresse / point relais
- Possible AVANT expédition uniquement
- Changement via notre outil logistique (pas rétroactif sur le compte client)
- La modification n'apparaît pas dans l'email de confirmation mais est prise en compte dans le lien de suivi
- Si commande en Chrono2Shop : proposer un point relais Chrono2Shop alternatif
- Si adresse incomplète pour point relais : demander au client via https://www.chronopost.fr/expeditionAvanceeSec/ounoustrouver.html

### Statuts Wing
- Nouveau : commande passée, non traitée
- En collecte : ajoutée à une collecte, non préparée
- Expédié : commande expédiée (lien de suivi disponible)
- Livré : commande livrée
- Retour : retour en cours

---

## STOCK & RÉASSORT

### Principes
- Collections limitées avec réassorts ciblés
- Certaines pointures peuvent ne pas revenir
- Inviter à s'abonner à l'alerte pointure sur la fiche produit
- Codes promo valables une seule fois, non rééditables

### Avoirs
- L'avoir est utilisable pour le même produit et la même couleur (sauf modification de stock)
- Validité : 6 mois
- Non cumulable avec d'autres codes
- Code promo "BEBELT" : offre ceinture offerte avec paire de chaussures

---

## POLITIQUE COMMERCIALE

### Remboursements
- Délai légal : 14 jours après réception
- Hors délai : avoir ou échange uniquement
- Si le client retourne une commande reçue en promotion : avoir au prix promotionnel payé
- Si colis non récupéré : avoir uniquement (pas de remboursement)

### Codes promo & réductions
- Codes générés automatiquement : impossibles à retrouver une fois expirés
- Code promo "metstoibien" : -20€ sur commande ≥99€, valable 7 jours
- Codes cadeaux : valables sur chaussures et ceintures pour un minimum de 99€
- Ne pas appliquer un code rétroactivement après commande passée

### Boutique physique
- Plus de boutique physique (ex-rue Saint-Placide, Paris)
- Vente en ligne uniquement

---

## LIENS UTILES
- Retours : https://maxsauveur.com/pages/livraison-et-retour
- Garantie à vie des semelles : https://maxsauveur.com/pages/la-garantie-a-vie-de-nos-semelles
- Trouver un point relais Chronopost : https://www.chronopost.fr/expeditionAvanceeSec/ounoustrouver.html

---

## SITUATIONS FRÉQUENTES & RÉPONSES TYPE

### Client demande où est sa commande
- Vérifier statut Shopify + lien de suivi Wing
- Si statut "Nouveau" ou "En collecte" : indiquer délai prévu
- Si expédié : donner le lien de suivi
- Si retard atelier/fournisseur : expliquer honnêtement, s'excuser

### Client demande à changer de point relais
- Vérifier si commande expédiée ou non
- Si non expédiée : possible, demander adresse complète du nouveau point relais
- Si Chrono2Shop → proposer uniquement un autre point Chrono2Shop
- Effectuer le changement dans Wing (outil logistique)
- Prévenir que la modification n'apparaîtra pas dans l'email de confirmation

### Client demande taille pour une paire
- Toujours demander la longueur du pied en cm (méthode du site)
- Adapter selon le modèle et les caractéristiques du pied (large, cou-de-pied fort, etc.)
- Éviter d'utiliser les pointures de sneakers comme référence

### Échange de taille
- Retour → avoir → recommande dans la bonne taille
- Si échange urgent avant Noël : conseiller de commander immédiatement la bonne taille et retourner l'ancienne en parallèle (remboursement à réception)

### Réassort
- Ne jamais s'engager sur une date ferme si incertain
- Inviter à l'alerte pointure
- Si on a une info : partager (ex: "réassort prévu fin janvier")

### Défaut / réparation
- Demander une photo
- Si défaut avéré : prise en charge retour + atelier
- Si usure normale (semelle usée, lacet cassé) : orienter vers cordonnier ou réparation payante

### Retour incomplet
- Ne pas rembourser tant que tout n'est pas reçu
- Informer clairement le client

### Avoir non fonctionnel
- Vérifier que les conditions sont respectées (même produit, min 99€, non expiré)
- Si erreur de notre côté : ajuster les paramètres du code

---

## EXPÉDITION — CALENDRIER

- Commande passée avant mardi 11h → expédiée le mardi
- Commande passée après mardi 11h → expédiée le jeudi
- Livraison standard : 3-5 jours ouvrés
- Livraison express : 1-2 jours ouvrés (commande avant 11h)

---

## PROMOTIONS & CAS SPÉCIAUX

### Code BEBELT / ceinture offerte
- Code BEBELT : ceinture offerte (ou -150€ sur ceinture) avec achat d'une paire
- Conditions : panier doit contenir une paire de chaussures + une ceinture
- Si le client dit que le code ne fonctionne pas : vérifier que les 2 articles sont bien dans le panier, que la ceinture est en stock, que la date limite n'est pas dépassée
- Si prix ceinture > 150€ : réduction partielle uniquement

### Promotions -25% et braderie
- Pas de retour possible sauf défaut de fabrication avéré
- Non rétroactives : impossible d'appliquer un code après commande passée

### Ceinture offerte + retour chaussures
- Si le client retourne les chaussures auxquelles la ceinture était associée → déduire le prix réel de la ceinture de l'avoir

---

## PRODUITS — INFOS COMPLÉMENTAIRES

### Embauchoirs
- Kit embauchoir disponible en 45 uniquement (pas de 46)
- Réponse type : "Nos embauchoirs ne sont pas disponibles en taille 46, uniquement en 45 dans notre kit."

### Cuir — usure normale vs défaut
- Marques de frottement sur le cuir = matière vivante, normal
- Doublure talon usée = usure liée à l'utilisation, pas couvert
- Réponse type : "Le cuir est une matière naturelle qui vit et marque selon l'usage. Il s'agit d'une usure normale et non d'un défaut de fabrication."

### Réparation Portugal
- En cas de défaut de fabrication avéré : réparation possible, délai ≈ 6-8 semaines (atelier logistique ou Portugal selon cas)

---

## FORMULATIONS TYPES VALIDÉES

### Changement adresse confirmé
"C'est fait, nous venons de modifier l'adresse de livraison dans notre outil logistique avec les informations que vous nous avez transmises. Notez que cette modification n'apparaîtra pas dans votre email de confirmation initial, mais elle est bien prise en compte et visible sur votre lien de suivi."

### Échange de taille
"Pas de souci. Vous pouvez nous retourner la paire pour un échange de pointure. Voici la procédure : emballez soigneusement la paire, ajoutez le bon de retour disponible sur maxsauveur.com/pages/livraison-et-retour en cochant 'avoir', et envoyez-la à notre adresse logistique. Dès réception et contrôle, nous vous enverrons un avoir pour commander la bonne taille."

### Rupture de stock
"Suite à une mauvaise synchronisation des stocks, ce modèle n'est plus disponible dans cette taille. Nous pouvons vous proposer soit de choisir un autre modèle ou coloris disponible, soit de recevoir un avoir valable 6 mois sur notre site. Laquelle de ces options vous convient le mieux ?"

### Défaut naturel (cuir)
"Le cuir est une matière naturelle qui vit et peut marquer selon l'usage. Après examen des photos, il s'agit d'une usure liée à l'utilisation et non d'un défaut de fabrication. Nous vous conseillons un lait nettoyant pour cuir pour entretenir la paire."

### Remboursement hors délai
"Malheureusement, le délai légal de 14 jours est dépassé. Nous ne pouvons donc pas procéder à un remboursement, mais nous pouvons vous proposer un avoir du montant de votre commande, valable 6 mois sur notre site."

### Colis mis à disposition au point relais
"De notre côté, nous voyons que le colis est bien mis à disposition au point relais depuis [date]. Pourriez-vous vérifier auprès du point relais ou via le lien de suivi ? Si le problème persiste, n'hésitez pas à nous revenir."
"""
