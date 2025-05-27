from app import create_app

app = create_app()

# Voeg deze regels toe om het script direct uitvoerbaar te maken in Spyder
if __name__ == '__main__':
    # We gebruiken poort 5001 en use_reloader=False om conflicten met Spyder te voorkomen
    app.run(port=5001, debug=True, use_reloader=False)