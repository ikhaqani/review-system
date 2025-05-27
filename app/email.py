def send_credentials_email(user, password):
    """
    SIMULATIE van e-mail verzenden.
    In een productieomgeving zou u hier een bibliotheek als Flask-Mail gebruiken
    om een echte e-mail te sturen via een SMTP-server (bv. SendGrid, Mailgun, Gmail).
    """
    print("--- SIMULATED EMAIL ---")
    print(f"To: {user.email}")
    print("From: noreply@review-system.com")
    print("Subject: Uw inloggegevens voor het Review Systeem")
    print("-" * 23)
    print(f"Beste gebruiker,\n")
    print("Hierbij uw inloggegevens voor het openEHR Review Systeem:")
    print(f"  Gebruikersnaam: {user.username}")
    print(f"  Wachtwoord: {password}\n")
    print("Bewaar deze gegevens goed.")
    print("--- END SIMULATED EMAIL ---")