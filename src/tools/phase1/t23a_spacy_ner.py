"""T23a: spaCy Named Entity Recognition - Minimal Implementation

Extracts named entities from text using spaCy's pre-trained models.
Core component of the vertical slice workflow for entity identification.

Minimal implementation focusing on:
- Standard entity types (PERSON, ORG, GPE, etc.)
- Basic confidence scoring (0.85 for spaCy)
- Mention creation with positions
- Integration with T107 Identity Service

Deferred features:
- Custom entity types
- LLM-based entity extraction (T23b)
- Advanced confidence modeling
- Multi-language support
"""

from typing import Dict, List, Optional, Any, Tuple
import uuid
from datetime import datetime
import spacy
from spacy.lang.en import English

# Import core services
from src.core.identity_service import IdentityService
from src.core.provenance_service import ProvenanceService
from src.core.quality_service import QualityService


class SpacyNER:
    """T23a: spaCy Named Entity Recognition."""
    
    def __init__(
        self,
        identity_service: IdentityService,
        provenance_service: ProvenanceService,
        quality_service: QualityService
    ):
        self.identity_service = identity_service
        self.provenance_service = provenance_service
        self.quality_service = quality_service
        self.tool_id = "T23A_SPACY_NER"
        
        # Lazy load spaCy model (only when needed)
        self.nlp = None
        self._model_initialized = False
        
        # Standard entity types to extract
        self.target_entity_types = {
            "PERSON",     # People, including fictional
            "ORG",        # Companies, agencies, institutions
            "GPE",        # Countries, cities, states
            "PRODUCT",    # Objects, vehicles, foods, etc.
            "EVENT",      # Named hurricanes, battles, wars, sports events
            "WORK_OF_ART", # Titles of books, songs, etc.
            "LAW",        # Named documents made into laws
            "LANGUAGE",   # Any named language
            "FACILITY",   # Buildings, airports, highways, bridges
            "MONEY",      # Monetary values
            "DATE",       # Absolute or relative dates or periods
            "TIME"        # Times smaller than a day
        }
        
        # Base confidence for spaCy extractions
        self.base_confidence = 0.85
    
    def _initialize_spacy_model(self):
        """Initialize spaCy model with error handling (lazy loading)."""
        if self._model_initialized:
            return
            
        try:
            # Try to load the medium English model first
            try:
                self.nlp = spacy.load("en_core_web_sm")
            except OSError:
                # If not available, try the small model
                try:
                    self.nlp = spacy.load("en_core_web_sm")
                except OSError:
                    # Create a blank English model as fallback
                    self.nlp = English()
                    print("Warning: No spaCy model found. Using blank model. Install with: python -m spacy download en_core_web_sm")
            
            self._model_initialized = True
                    
        except Exception as e:
            print(f"Error initializing spaCy: {e}")
            self.nlp = None
            self._model_initialized = False
    
    def extract_entities(
        self,
        chunk_ref: str,
        text: str,
        chunk_confidence: float = 0.8
    ) -> Dict[str, Any]:
        """Extract named entities from text chunk.
        
        Args:
            chunk_ref: Reference to source text chunk
            text: Text to analyze
            chunk_confidence: Confidence score from chunk
            
        Returns:
            List of extracted entities with positions and confidence
        """
        # Start operation tracking
        operation_id = self.provenance_service.start_operation(
            tool_id=self.tool_id,
            operation_type="extract_entities",
            inputs=[chunk_ref],
            parameters={
                "text_length": len(text),
                "model": "spacy_en"
            }
        )
        
        try:
            # Initialize spaCy model only when needed
            self._initialize_spacy_model()
            
            if not self.nlp:
                return self._complete_with_error(
                    operation_id,
                    "spaCy model not available"
                )
            
            # Input validation
            if not text or not text.strip():
                return self._complete_with_error(
                    operation_id,
                    "Text cannot be empty"
                )
            
            if not chunk_ref:
                return self._complete_with_error(
                    operation_id,
                    "chunk_ref is required"
                )
            
            if not self.nlp:
                return self._complete_with_error(
                    operation_id,
                    "spaCy model not available"
                )
            
            # Process text with spaCy
            doc = self.nlp(text)
            
            # Extract entities
            extracted_entities = []
            mention_refs = []
            
            for ent in doc.ents:
                # Filter to target entity types
                if ent.label_ not in self.target_entity_types:
                    continue
                
                # Skip very short entities (likely noise)
                if len(ent.text.strip()) < 2:
                    continue
                
                # Calculate entity confidence
                entity_confidence = self._calculate_entity_confidence(
                    entity_text=ent.text,
                    entity_type=ent.label_,
                    context_confidence=chunk_confidence
                )
                
                # Create mention through identity service
                mention_result = self.identity_service.create_mention(
                    surface_form=ent.text,
                    start_pos=ent.start_char,
                    end_pos=ent.end_char,
                    source_ref=chunk_ref,
                    entity_type=ent.label_,
                    confidence=entity_confidence
                )
                
                if mention_result["status"] == "success":
                    entity_data = {
                        "mention_id": mention_result["mention_id"],
                        "entity_id": mention_result["entity_id"],
                        "mention_ref": f"storage://mention/{mention_result['mention_id']}",
                        "surface_form": ent.text,
                        "normalized_form": mention_result["normalized_form"],
                        "entity_type": ent.label_,
                        "start_char": ent.start_char,
                        "end_char": ent.end_char,
                        "confidence": entity_confidence,
                        "source_chunk": chunk_ref,
                        "extraction_method": "spacy_ner",
                        "created_at": datetime.now().isoformat()
                    }
                    
                    extracted_entities.append(entity_data)
                    mention_refs.append(entity_data["mention_ref"])
                    
                    # Assess quality for mention
                    quality_result = self.quality_service.assess_confidence(
                        object_ref=entity_data["mention_ref"],
                        base_confidence=entity_confidence,
                        factors={
                            "entity_length": min(1.0, len(ent.text) / 20),  # Longer entities better
                            "entity_type_confidence": self._get_type_confidence(ent.label_),
                            "context_quality": chunk_confidence
                        },
                        metadata={
                            "extraction_tool": "spacy",
                            "entity_type": ent.label_,
                            "source_chunk": chunk_ref
                        }
                    )
                    
                    if quality_result["status"] == "success":
                        entity_data["quality_confidence"] = quality_result["confidence"]
                        entity_data["quality_tier"] = quality_result["quality_tier"]
            
            # Complete operation
            completion_result = self.provenance_service.complete_operation(
                operation_id=operation_id,
                outputs=mention_refs,
                success=True,
                metadata={
                    "entities_extracted": len(extracted_entities),
                    "text_length": len(text),
                    "entity_types": list(set(e["entity_type"] for e in extracted_entities))
                }
            )
            
            return {
                "status": "success",
                "entities": extracted_entities,
                "total_entities": len(extracted_entities),
                "entity_types": self._count_entity_types(extracted_entities),
                "operation_id": operation_id,
                "provenance": completion_result
            }
            
        except Exception as e:
            return self._complete_with_error(
                operation_id,
                f"Unexpected error during entity extraction: {str(e)}"
            )
    
    def _calculate_entity_confidence(
        self, 
        entity_text: str, 
        entity_type: str, 
        context_confidence: float
    ) -> float:
        """Calculate confidence for an extracted entity."""
        base_conf = self.base_confidence
        
        # Adjust based on entity characteristics
        factors = []
        
        # Length factor (longer entities usually more reliable)
        if len(entity_text) > 10:
            factors.append(0.95)
        elif len(entity_text) > 5:
            factors.append(0.9)
        else:
            factors.append(0.8)
        
        # Entity type confidence
        type_conf = self._get_type_confidence(entity_type)
        factors.append(type_conf)
        
        # Context confidence
        factors.append(context_confidence)
        
        # Calculate weighted average
        if factors:
            final_confidence = (base_conf + sum(factors)) / (len(factors) + 1)
        else:
            final_confidence = base_conf
        
        return max(0.1, min(1.0, final_confidence))
    
    def _get_type_confidence(self, entity_type: str) -> float:
        """Get confidence modifier for entity type."""
        # Some entity types are more reliable than others
        type_confidences = {
            "PERSON": 0.9,      # Names are usually reliable
            "ORG": 0.85,        # Organizations quite reliable
            "GPE": 0.9,         # Geographic entities reliable
            "PRODUCT": 0.8,     # Products can be ambiguous
            "EVENT": 0.8,       # Events can be ambiguous
            "WORK_OF_ART": 0.75, # Can be subjective
            "LAW": 0.85,        # Laws are usually precise
            "LANGUAGE": 0.9,    # Languages are clear
            "FACILITY": 0.85,   # Facilities usually clear
            "MONEY": 0.95,      # Money amounts very reliable
            "DATE": 0.9,        # Dates usually reliable
            "TIME": 0.9         # Times usually reliable
        }
        
        return type_confidences.get(entity_type, 0.8)  # Default confidence
    
    def _count_entity_types(self, entities: List[Dict[str, Any]]) -> Dict[str, int]:
        """Count entities by type."""
        type_counts = {}
        for entity in entities:
            entity_type = entity["entity_type"]
            type_counts[entity_type] = type_counts.get(entity_type, 0) + 1
        return type_counts
    
    def _complete_with_error(self, operation_id: str, error_message: str) -> Dict[str, Any]:
        """Complete operation with error."""
        self.provenance_service.complete_operation(
            operation_id=operation_id,
            outputs=[],
            success=False,
            error_message=error_message
        )
        
        return {
            "status": "error",
            "error": error_message,
            "operation_id": operation_id
        }
    
    def get_supported_entity_types(self) -> List[str]:
        """Get list of supported entity types."""
        return list(self.target_entity_types)
    
    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the loaded spaCy model."""
        if not self.nlp:
            return {"model": "none", "available": False}
        
        try:
            return {
                "model": self.nlp.meta.get("name", "unknown"),
                "version": self.nlp.meta.get("version", "unknown"),
                "language": self.nlp.meta.get("lang", "en"),
                "available": True,
                "pipeline": list(self.nlp.pipe_names)
            }
        except:
            return {"model": "basic", "available": True}
    
    def get_tool_info(self) -> Dict[str, Any]:
        """Get tool information."""
        return {
            "tool_id": self.tool_id,
            "name": "spaCy Named Entity Recognition",
            "version": "1.0.0",
            "description": "Extracts named entities using spaCy pre-trained models",
            "supported_entity_types": list(self.target_entity_types),
            "base_confidence": self.base_confidence,
            "model_info": self.get_model_info(),
            "input_type": "chunk",
            "output_type": "mentions"
        }