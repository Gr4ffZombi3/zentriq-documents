"""Haeufige Vornamen (klein geschrieben) fuer die lokale Namenserkennung beim Anonymisieren.

Eine feste Liste statt eines Sprachmodells: Die Erkennung laeuft vollstaendig auf dem Server,
ohne externe KI. Ein Vorname allein wird nie ersetzt - nur "Vorname Nachname" bzw.
"Nachname, Vorname" (app/services/anonymize.py)."""

FIRST_NAMES = frozenset(
    """
    aaron adam adrian ahmet aileen alena alessandro alex alexa alexander alexandra alexej alfred ali
    alice alina aline alisa alma amelie ana andre andrea andreas andrej andrzej angela angelika angelina
    anita anja anke ann anna annabell anne annegret annemarie annette annika anton antonia arne arnd
    arnold artur astrid aylin ayse barbara bastian beate beatrix ben benedikt benjamin bernd bernhard
    berta bettina bianca birgit bjoern björn bodo boris brigitte bruno burak burkhard can carina carl
    carla carmen carolin caroline carsten celina cem charlotte chantal chiara christa christian christiane
    christina christine christoph christopher clara claudia claus constanze cornelia daniel daniela
    danny dariusz david denis deniz dennis detlef diana dieter dietmar dietrich dirk dominik dominika
    doris dorothea edith eduard elena elfriede elias elif elisa elisabeth elke ella ellen elmar elvira
    emil emilia emily emine emma emre engelbert enrico erik erika ernst esra eva evelyn ewa fabian fabio
    fatih fatma felix ferdinand finn florian frank franz franziska frauke frederik freya friederike
    friedrich fritz gabriele georg gerald gerd gerda gerhard gertrud gisela gregor greta gudrun guido
    gunnar gustav gunther günter günther hakan hanna hannah hannelore hannes hans hans-dieter
    hans-jürgen hans-joachim hans-peter harald hartmut hasan hatice heidi heike heiko heinrich heinz
    helena helene helga helmut hendrik henning henri henrik henry herbert hermann hilde hildegard holger
    horst hubert hüseyin ibrahim ida igor ilona ina inge ingeborg ingo ingrid irina iris irmgard isabel
    isabell isabella ivan jacqueline jakob jan jana janina janine jannik jasmin jens jessica joachim
    jochen johann johanna johannes jonas jonathan jörg jose josef josefine julia julian juliane julius
    jürgen justin jutta kai karin karina karl karl-heinz karolina katarzyna katharina kathrin katja
    katrin kemal kerstin kevin kim klaus konrad konstantin kristina kurt lara lars laura lea lena leon
    leonie liane lilli lina linda lisa lothar louis luca lucas lucia ludwig luis luisa lukas luise lydia
    magdalena maik malte mandy manfred manuel manuela marc marcel marco marcus maren margarete margit
    maria marian marianne marie marina mario marion marius mark marko markus marlene martha martin
    martina marvin mathias matthias max maximilian mehmet melanie melina melissa merve mia michael
    michaela michelle miriam mirko mohamed mohammed monika moritz murat mustafa nadine nadja natalia
    natalie natascha nico nicola nicole niklas nils nina noah norbert oliver olga oskar otto pascal
    patricia patrick paul paula pauline peter petra philipp philip piotr rainer ralf ralph rebecca
    regina reinhard reinhold renate rene rené richard rita robert robin roland rolf roman ronald rosa
    rosemarie rudolf ruth sabine sabrina sandra sara sarah sascha sebastian selin serkan sergej silke
    silvia simon simone sofia sonja sophia sophie stefan stefanie steffen stephan stephanie susanne sven
    svenja swen tanja tatjana theo theresa thomas thorsten tilo tim timo tina tobias tom tomasz torsten
    udo ulf ulla ulrich ulrike ursula uta ute uwe valentin vanessa vera verena veronika viktor viktoria
    vincent volker walter waltraud werner wilhelm willi wolfgang yannick yasemin yusuf zeynep
    """.split()
)
