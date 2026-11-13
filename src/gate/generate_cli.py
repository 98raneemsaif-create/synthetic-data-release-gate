"""CLI smoke test and CSV export; artifacts are unvalidated private candidates."""
import argparse
from datetime import datetime, timezone
from gate.reference import make_reference
from gate.generate import generate,export,GenerationError
from gate.canonical import canonical_reference_hash

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--backend',choices=['sdv','demo'],default='sdv')
    ap.add_argument('--customers',type=int,default=150)
    ap.add_argument('--seed',type=int,default=2026)
    ap.add_argument('--scale',type=float,default=1.)
    ap.add_argument('--out',default='artifacts/private_candidate')
    ap.add_argument('--generation-run-id', default=None)
    args=ap.parse_args()
    try:
        reference=make_reference(args.customers,args.seed)
        result=generate(reference,backend=args.backend,seed=args.seed,scale=args.scale)
        generation_run_id = args.generation_run_id or f'generation-{args.customers}-{args.seed}-{args.scale:g}'
        path=export(result,args.out,provenance={
            'generation_run_id': generation_run_id,
            'reference_parameters': {'customers': args.customers, 'seed': args.seed},
            'reference_hash': canonical_reference_hash(
                reference, customers=args.customers, seed=args.seed
            ),
            'generated_at': datetime.now(timezone.utc).isoformat(),
        })
    except (GenerationError,ValueError) as exc:
        ap.exit(2,f'ERROR (no output released): {exc}\n')
    print(f'Backend: {result.backend}; exported PRIVATE candidate to {path}')
    for name,frame in result.tables.items(): print(name,len(frame))

if __name__=='__main__':main()
