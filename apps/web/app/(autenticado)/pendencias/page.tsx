export default function PendenciasPage() {
  return (
    <div>
      <div className="page-head">
        <p className="eyebrow">Operação</p>
        <h1>Pendências</h1>
        <p>Sinalização de pendências de documentos antes do envio à operadora.</p>
      </div>

      <div className="panel">
        <div className="panel-heading">
          <h2>Em construção</h2>
          <span className="state state--off">Issue #8</span>
        </div>
        <p className="empty-state">
          Esta tela ainda não existe. Nenhuma pendência real ou fictícia é exibida aqui até que a
          funcionalidade seja implementada (issue #8).
        </p>
      </div>
    </div>
  );
}
