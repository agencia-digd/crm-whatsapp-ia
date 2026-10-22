import Link from "next/link";

export default function Home() {
  return (
    <div className="home">
      <h1>Painel Acme, versão 2</h1>
      <p className="lead">
        Reconstruído do zero, módulo a módulo. Por enquanto, um só — e é o que
        importa.
      </p>
      <Link href="/crm" className="mod-card">
        <div className="mod-title">CRM</div>
        <div className="mod-desc">
          Funil de leads em kanban, lendo a espinha canônica do Copiloto.
        </div>
        <div className="mod-go">Abrir CRM →</div>
      </Link>
    </div>
  );
}
